"""Gates a tool call: guardrails (hard deny) -> remembered grants -> ask.

The `ask` callback is intentionally decoupled from Textual so this module
stays testable without a running App; tui/screens/permission_modal.py
supplies the real implementation via `push_screen_wait`.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pcli.permissions.guardrails import GuardrailsConfig
from pcli.permissions.policy import PermissionPolicy
from pcli.session.models import PermissionGrant, Session

PermissionDecision = Literal["allow", "deny"]
RememberScope = Literal["once", "session", "always"]

AskCallback = Callable[[str, dict[str, Any], str], Awaitable[tuple[PermissionDecision, RememberScope | None]]]


class PermissionManager:
    def __init__(
        self,
        *,
        guardrails: GuardrailsConfig | None = None,
        policy: PermissionPolicy | None = None,
    ) -> None:
        self.guardrails = guardrails or GuardrailsConfig.load()
        self.policy = policy or PermissionPolicy()
        self._recent_tool_call_times: deque[float] = deque()

    def _within_rate_limit(self) -> bool:
        """guardrails.max_tool_calls_per_minute as a sliding 60s window,
        shared across every tool call this manager gates (including a
        subagent's, since it's handed the same PermissionManager instance) —
        a global rate cap independent of any single turn's own tool-call
        count (see max_tool_calls_per_turn in agent/loop.py)."""
        limit = self.guardrails.max_tool_calls_per_minute
        if limit <= 0:
            return True
        now = time.monotonic()
        cutoff = now - 60.0
        while self._recent_tool_call_times and self._recent_tool_call_times[0] < cutoff:
            self._recent_tool_call_times.popleft()
        if len(self._recent_tool_call_times) >= limit:
            return False
        self._recent_tool_call_times.append(now)
        return True

    async def check(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        command: str | None = None,
        path: str | None = None,
        python_module: str | None = None,
        ask: AskCallback | None = None,
        risk_description: str = "",
        default_allow: bool = False,
        session: Session | None = None,
    ) -> PermissionDecision:
        """`default_allow=True` is for tools that don't need a user prompt
        (e.g. read_file) but must still respect the hard guardrails below.

        `session`, if given, gets a PermissionGrant record appended whenever
        a "session" or "always" grant is remembered — a historical audit
        trail that travels with session export/import. It's independent of
        enforcement: "always" grants are enforced via self.policy (persisted
        separately in permissions.json), and are deliberately not re-applied
        from a session's own history on import, to avoid double-recording
        the same grant into permissions.json."""
        if not self._within_rate_limit():
            return "deny"

        if command is not None:
            result = self.guardrails.evaluate_command(command)
            if not result.allowed:
                return "deny"

        if path is not None:
            result = self.guardrails.evaluate_path(path)
            if not result.allowed:
                return "deny"

        if python_module is not None:
            result = self.guardrails.evaluate_python_module(python_module)
            if not result.allowed:
                return "deny"

        if default_allow:
            return "allow"

        existing = self.policy.check(tool_name)
        if existing is not None:
            return existing

        if ask is None:
            # No UI available to ask through -> fail closed.
            return "deny"

        decision, remember_scope = await ask(tool_name, arguments, risk_description)
        if remember_scope is not None and remember_scope != "once":
            self.policy.remember(tool_name, scope=remember_scope, decision=decision)
            if session is not None:
                session.permission_grants.append(
                    PermissionGrant(tool_name=tool_name, scope=remember_scope, decision=decision)
                )
        return decision
