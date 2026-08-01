"""Runs arbitrary Python (module introspection or a function call) in an
isolated subprocess, never in the main pcli process.

Deliberately does NOT go through ctx.sandbox: that may be a Docker container
running a bare image with none of the host's installed packages, whereas
pydiscovery's whole point is to reach packages installed in the *host*
environment. So this always spawns the host interpreter (sys.executable)
through a dedicated RestrictedSubprocessSandbox instead — real process
isolation (scrubbed env, timeout, resource limits) without losing access to
the packages being introspected.
"""

from __future__ import annotations

import json
import sys

from pcli.sandbox.base import ExecRequest
from pcli.sandbox.subprocess_backend import RestrictedSubprocessSandbox
from pcli.tools.base import ToolContext, ToolResult, ToolSpec

_BOOTSTRAP_SCRIPT = r"""
import importlib, inspect, json, sys

def _resolve(qualified_name):
    parts = qualified_name.split(".")
    module = None
    remaining = []
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        try:
            module = importlib.import_module(candidate)
            remaining = parts[i:]
            break
        except ImportError:
            continue
    if module is None:
        raise ImportError("Could not import any prefix of %r" % (qualified_name,))
    obj = module
    for attr in remaining:
        obj = getattr(obj, attr)
    return obj

def _json_safe(value):
    try:
        json.dumps(value)
        return value
    except TypeError:
        return {"__repr__": repr(value)[:2000], "__type__": type(value).__name__}

def main():
    payload = json.loads(sys.stdin.read())
    mode = payload["mode"]
    if mode == "inspect":
        module = importlib.import_module(payload["module"])
        query = (payload.get("query") or "").lower()
        members = []
        for name, obj in inspect.getmembers(module):
            if name.startswith("_"):
                continue
            if query and query not in name.lower():
                continue
            if not (inspect.isfunction(obj) or inspect.isclass(obj)
                    or inspect.isbuiltin(obj) or inspect.ismethod(obj)):
                continue
            try:
                sig = str(inspect.signature(obj))
            except (ValueError, TypeError):
                sig = "(...)"
            doc_lines = (inspect.getdoc(obj) or "").strip().splitlines()
            members.append({
                "name": name,
                "kind": "class" if inspect.isclass(obj) else "function",
                "signature": sig,
                "doc": doc_lines[0] if doc_lines else "",
            })
            if len(members) >= 200:
                break
        print(json.dumps({"ok": True, "members": members}))
    elif mode == "call":
        obj = _resolve(payload["qualified_name"])
        if not callable(obj):
            print(json.dumps({"ok": False, "error": "%r is not callable" % (payload["qualified_name"],)}))
            return
        result = obj(*(payload.get("args") or []), **(payload.get("kwargs") or {}))
        print(json.dumps({"ok": True, "result": _json_safe(result)}))
    else:
        print(json.dumps({"ok": False, "error": "unknown mode %r" % (mode,)}))

try:
    main()
except Exception as exc:
    print(json.dumps({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}))
"""


async def run_bootstrap(ctx: ToolContext, payload: dict, *, timeout_s: float = 30.0) -> dict:
    sandbox = RestrictedSubprocessSandbox(allowed_roots=[ctx.cwd])
    request = ExecRequest(
        command=[sys.executable, "-c", _BOOTSTRAP_SCRIPT],
        cwd=ctx.cwd,
        stdin=json.dumps(payload),
        timeout_s=timeout_s,
    )
    result = await sandbox.execute(request)
    if result.timed_out:
        raise RuntimeError("timed out")
    output_line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if not output_line:
        raise RuntimeError(result.stderr.strip() or f"no output (exit_code={result.exit_code})")
    try:
        return json.loads(output_line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not parse bootstrap output: {result.stdout!r}") from exc


async def _call_python(arguments: dict, ctx: ToolContext) -> ToolResult:
    qualified_name = arguments["qualified_name"]
    payload = {
        "mode": "call",
        "qualified_name": qualified_name,
        "args": arguments.get("args") or [],
        "kwargs": arguments.get("kwargs") or {},
    }
    try:
        data = await run_bootstrap(ctx, payload)
    except RuntimeError as exc:
        return ToolResult(output=f"Call to '{qualified_name}' failed: {exc}", is_error=True)

    if not data.get("ok"):
        return ToolResult(output=data.get("error", "unknown error"), is_error=True)

    result = data["result"]
    if isinstance(result, dict) and set(result) == {"__repr__", "__type__"}:
        text = f"<{result['__type__']}> {result['__repr__']}"
    elif isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, indent=2)
    return ToolResult(output=text)


CALL_PYTHON = ToolSpec(
    name="call_python",
    description="Call a Python function by its fully-qualified name (e.g. 'math.sqrt', "
    "'json.dumps'), with JSON-serializable positional/keyword arguments. Runs in an "
    "isolated subprocess with the host's installed packages, not the main pcli process.",
    parameters={
        "type": "object",
        "properties": {
            "qualified_name": {
                "type": "string",
                "description": "Fully-qualified function/callable name, e.g. 'math.sqrt'.",
            },
            "args": {"type": "array", "description": "Positional arguments (JSON values)."},
            "kwargs": {"type": "object", "description": "Keyword arguments (JSON values)."},
        },
        "required": ["qualified_name"],
    },
    handler=_call_python,
    needs_permission=True,
    needs_sandbox=True,
    risk_description="Executes an arbitrary Python function call in a subprocess.",
    guardrail_python_module_arg="qualified_name",
)
