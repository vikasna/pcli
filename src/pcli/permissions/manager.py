"""Gates a tool call: guardrails (hard deny) -> remembered grants -> ask.

The `ask` callback is intentionally decoupled from Textual so this module
stays testable without a running App; tui/screens/permission_modal.py
supplies the real implementation via `push_screen_wait`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pcli.permissions.guardrails import GuardrailsConfig
from pcli.permissions.policy import PermissionPolicy

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
    ) -> PermissionDecision:
        """`default_allow=True` is for tools that don't need a user prompt
        (e.g. read_file) but must still respect the hard guardrails below."""
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
        return decision
