"""search_python: coarse, in-process, name/summary-only search (tier 1) —
safe, no imports, no permission needed.

inspect_python_module: imports one specific module to list its public
functions/classes (tier 2) — this DOES import third-party code, so it's
permission-gated and runs through invoke.run_bootstrap's isolated subprocess.
"""

from __future__ import annotations

from pcli.tools.base import ToolContext, ToolResult, ToolSpec
from pcli.tools.pydiscovery.cache import load_or_build_index
from pcli.tools.pydiscovery.invoke import run_bootstrap

_MAX_RESULTS = 50


async def _search_python(arguments: dict, ctx: ToolContext) -> ToolResult:
    query = arguments.get("query", "").strip().lower()
    entries = load_or_build_index()
    matches = [
        e for e in entries if not query or query in e.name.lower() or query in e.summary.lower()
    ]

    if not matches:
        return ToolResult(output="No matching modules/packages found.")

    lines = [f"{e.name} - {e.summary}" if e.summary else e.name for e in matches[:_MAX_RESULTS]]
    text = "\n".join(lines)
    if len(matches) > _MAX_RESULTS:
        text += f"\n[...{len(matches) - _MAX_RESULTS} more matches not shown, narrow your query...]"
    text += (
        "\n\n(Name/summary index only — nothing was imported. Use inspect_python_module to "
        "see the functions/classes inside a specific module.)"
    )
    return ToolResult(output=text)


SEARCH_PYTHON = ToolSpec(
    name="search_python",
    description="Search installed Python packages/modules (stdlib + site-packages) by name or "
    "summary, without importing anything. Use this first to find candidate modules, then "
    "inspect_python_module to see what's inside one.",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keyword(s) to match against module names and package summaries. "
                "Empty string lists everything.",
            }
        },
        "required": ["query"],
    },
    handler=_search_python,
    needs_permission=False,
    plan_mode_safe=True,
)


async def _inspect_python_module(arguments: dict, ctx: ToolContext) -> ToolResult:
    module = arguments["module"]
    payload = {"mode": "inspect", "module": module, "query": arguments.get("query", "")}
    try:
        data = await run_bootstrap(ctx, payload)
    except RuntimeError as exc:
        return ToolResult(
            output=f"Failed to inspect module '{module}': {exc}\n"
            "[pcli] Suggestion: run search_python first to confirm the exact module name is "
            "actually installed.",
            is_error=True,
        )

    if not data.get("ok"):
        return ToolResult(
            output=f"{data.get('error', 'unknown error')}\n"
            "[pcli] Suggestion: run search_python first to confirm the exact module name is "
            "actually installed.",
            is_error=True,
        )

    members = data["members"]
    if not members:
        return ToolResult(output=f"No matching members found in module '{module}'.")
    lines = [
        f"{m['kind']} {m['name']}{m['signature']}" + (f" - {m['doc']}" if m["doc"] else "")
        for m in members
    ]
    return ToolResult(output="\n".join(lines))


INSPECT_PYTHON_MODULE = ToolSpec(
    name="inspect_python_module",
    description="Import and introspect a specific Python module to see its public functions "
    "and classes (name, signature, one-line docstring). This actually imports the module "
    "(in an isolated subprocess), so use search_python first to find the right module name.",
    parameters={
        "type": "object",
        "properties": {
            "module": {
                "type": "string",
                "description": "Fully-qualified module name to import, e.g. 'json' or "
                "'xml.etree.ElementTree'.",
            },
            "query": {"type": "string", "description": "Optional keyword to filter member names."},
        },
        "required": ["module"],
    },
    handler=_inspect_python_module,
    needs_permission=True,
    needs_sandbox=True,
    risk_description="Imports a Python module, which may execute module-level side effects.",
    guardrail_python_module_arg="module",
    plan_mode_safe=True,
)
