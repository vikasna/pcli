from pathlib import Path

import pytest

from pcli.permissions.guardrails import GuardrailsConfig
from pcli.permissions.manager import PermissionManager
from pcli.permissions.policy import PermissionPolicy


def _guardrails(**overrides) -> GuardrailsConfig:
    defaults = {
        "shell_denylist": ["rm -rf /", "mkfs*"],
        "fs_allowed_roots": ["/allowed"],
        "fs_deny_paths": ["/allowed/.secret"],
    }
    defaults.update(overrides)
    return GuardrailsConfig(**defaults)


def test_guardrails_evaluate_command_denylist_substring():
    guardrails = _guardrails()
    assert guardrails.evaluate_command("sudo rm -rf / --no-preserve-root").allowed is False
    assert guardrails.evaluate_command("git status").allowed is True


def test_guardrails_evaluate_command_denylist_wildcard():
    guardrails = _guardrails()
    result = guardrails.evaluate_command("mkfs.ext4 /dev/sdb1")
    assert result.allowed is False
    assert "mkfs*" in result.reason


def test_guardrails_evaluate_path_outside_allowed_roots():
    guardrails = _guardrails()
    result = guardrails.evaluate_path("/somewhere/else/file.txt")
    assert result.allowed is False


def test_guardrails_evaluate_path_within_deny_path():
    guardrails = _guardrails()
    result = guardrails.evaluate_path("/allowed/.secret/token")
    assert result.allowed is False
    assert "denied path" in result.reason


def test_guardrails_evaluate_path_allowed():
    guardrails = _guardrails()
    result = guardrails.evaluate_path("/allowed/project/file.txt")
    assert result.allowed is True


def test_permission_policy_remember_and_check_session_scope(tmp_path: Path):
    policy = PermissionPolicy(persist_path=tmp_path / "permissions.json")
    assert policy.check("run_shell") is None

    policy.remember("run_shell", scope="session", decision="allow")
    assert policy.check("run_shell") == "allow"

    # A fresh policy instance shouldn't see session-scoped grants (in-memory only).
    policy2 = PermissionPolicy(persist_path=tmp_path / "permissions.json")
    assert policy2.check("run_shell") is None


def test_permission_policy_remember_always_persists(tmp_path: Path):
    persist_path = tmp_path / "permissions.json"
    policy = PermissionPolicy(persist_path=persist_path)
    policy.remember("run_shell", scope="always", decision="allow")
    assert persist_path.exists()

    policy2 = PermissionPolicy(persist_path=persist_path)
    assert policy2.check("run_shell") == "allow"


@pytest.mark.asyncio
async def test_permission_manager_denies_on_guardrail_violation(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    decision = await manager.check(
        "run_shell", {"command": "rm -rf /"}, command="rm -rf /", ask=None
    )
    assert decision == "deny"


@pytest.mark.asyncio
async def test_permission_manager_fails_closed_without_ask(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    decision = await manager.check("run_shell", {"command": "git status"}, ask=None)
    assert decision == "deny"


@pytest.mark.asyncio
async def test_permission_manager_asks_and_remembers_for_session(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )

    async def ask(tool_name, arguments, risk_description):
        return ("allow", "session")

    decision = await manager.check("run_shell", {"command": "git status"}, ask=ask)
    assert decision == "allow"

    # Second call should hit the remembered grant and not need to ask again.
    async def ask_should_not_be_called(tool_name, arguments, risk_description):
        raise AssertionError("ask() should not be called once a grant is remembered")

    decision2 = await manager.check(
        "run_shell", {"command": "git status"}, ask=ask_should_not_be_called
    )
    assert decision2 == "allow"


@pytest.mark.asyncio
async def test_permission_manager_allow_once_does_not_remember(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )

    async def ask(tool_name, arguments, risk_description):
        return ("allow", "once")

    decision = await manager.check("run_shell", {"command": "git status"}, ask=ask)
    assert decision == "allow"
    assert manager.policy.check("run_shell") is None
