from pathlib import Path

import pytest

from pcli.permissions.guardrails import GuardrailsConfig
from pcli.permissions.manager import PermissionManager
from pcli.permissions.policy import PermissionPolicy
from pcli.session.models import Session


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


def test_guardrails_evaluate_path_outside_allowed_roots_suggests_the_fix(tmp_path: Path):
    """Regression coverage for a real reported gap: the old message ("path
    'X' is outside all allowed roots") gave no indication this is a
    config-driven guardrail (not a permission choice a prompt could
    override), or how to actually fix it."""
    guardrails = _guardrails()
    result = guardrails.evaluate_path("/somewhere/else/file.txt")
    assert result.allowed is False
    assert "[pcli] Suggestion:" in result.reason
    assert "guardrails.toml" in result.reason
    assert "allowed_roots" in result.reason
    assert "/allowed-roots add" in result.reason
    assert "checked before any permission prompt" in result.reason


def test_guardrails_evaluate_path_within_deny_path():
    guardrails = _guardrails()
    result = guardrails.evaluate_path("/allowed/.secret/token")
    assert result.allowed is False
    assert "denied path" in result.reason


def test_guardrails_evaluate_path_within_deny_path_explains_why_not_just_that(tmp_path: Path):
    guardrails = _guardrails()
    result = guardrails.evaluate_path("/allowed/.secret/token")
    assert result.allowed is False
    assert "[pcli] Suggestion:" in result.reason
    assert "guardrails.toml" in result.reason
    assert "deny_paths" in result.reason
    assert "checked before any permission prompt" in result.reason
    # Deliberately no "/allowed-roots add"-style one-liner here - this is a
    # sensitive-path block (SSH keys, credentials, ...), not routine
    # working-directory friction, and shouldn't read as trivially bypassable.
    assert "/allowed-roots" not in result.reason


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


@pytest.mark.asyncio
async def test_permission_manager_records_grant_on_session(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    session = Session()
    assert session.permission_grants == []

    async def ask(tool_name, arguments, risk_description):
        return ("allow", "always")

    decision = await manager.check(
        "run_shell", {"command": "git status"}, ask=ask, session=session
    )

    assert decision == "allow"
    assert len(session.permission_grants) == 1
    grant = session.permission_grants[0]
    assert grant.tool_name == "run_shell"
    assert grant.scope == "always"
    assert grant.decision == "allow"


@pytest.mark.asyncio
async def test_permission_manager_does_not_record_grant_for_allow_once(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    session = Session()

    async def ask(tool_name, arguments, risk_description):
        return ("allow", "once")

    await manager.check("run_shell", {"command": "git status"}, ask=ask, session=session)
    assert session.permission_grants == []


@pytest.mark.asyncio
async def test_permission_manager_enforces_rate_limit(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(max_tool_calls_per_minute=2),
        policy=PermissionPolicy(persist_path=tmp_path / "p.json"),
    )

    async def ask(tool_name, arguments, risk_description):
        return ("allow", "once")

    first = await manager.check("run_shell", {"command": "git status"}, ask=ask)
    second = await manager.check("run_shell", {"command": "git status"}, ask=ask)
    third = await manager.check("run_shell", {"command": "git status"}, ask=ask)

    assert first == "allow"
    assert second == "allow"
    assert third == "deny"  # third call within the same 60s window exceeds the limit of 2


@pytest.mark.asyncio
async def test_permission_manager_rate_limit_disabled_when_zero(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(max_tool_calls_per_minute=0),
        policy=PermissionPolicy(persist_path=tmp_path / "p.json"),
    )

    async def ask(tool_name, arguments, risk_description):
        return ("allow", "once")

    for _ in range(5):
        decision = await manager.check("run_shell", {"command": "git status"}, ask=ask)
        assert decision == "allow"


# --- check_with_reason(): the enriched-deny-message mechanism ---
#
# Regression coverage for a real gap: a bare "Permission denied." gave the
# model nothing to diagnose, directly undercutting the "Recovering from a
# failed tool call" system-prompt guidance to diagnose before retrying.
# check() itself must keep returning a plain PermissionDecision unchanged
# (see the tests above, none of which needed updating) - check_with_reason()
# is the richer variant AgentLoop now calls instead.


@pytest.mark.asyncio
async def test_check_with_reason_surfaces_the_guardrail_command_reason(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    decision, reason = await manager.check_with_reason(
        "run_shell", {"command": "rm -rf /"}, command="rm -rf /", ask=None
    )
    assert decision == "deny"
    assert "rm -rf /" in reason


@pytest.mark.asyncio
async def test_check_with_reason_surfaces_the_guardrail_path_reason(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    decision, reason = await manager.check_with_reason(
        "write_file", {"path": "/somewhere/else/file.txt"}, path="/somewhere/else/file.txt", ask=None
    )
    assert decision == "deny"
    assert "outside all allowed roots" in reason


@pytest.mark.asyncio
async def test_check_with_reason_surfaces_the_guardrail_module_reason(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(python_module_denylist=["os"]),
        policy=PermissionPolicy(persist_path=tmp_path / "p.json"),
    )
    decision, reason = await manager.check_with_reason(
        "call_python", {"qualified_name": "os.system"}, python_module="os", ask=None
    )
    assert decision == "deny"
    assert "os" in reason


@pytest.mark.asyncio
async def test_check_with_reason_names_the_rate_limit(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(max_tool_calls_per_minute=1),
        policy=PermissionPolicy(persist_path=tmp_path / "p.json"),
    )

    async def ask(tool_name, arguments, risk_description):
        return ("allow", "once")

    await manager.check_with_reason("run_shell", {"command": "git status"}, ask=ask)
    decision, reason = await manager.check_with_reason(
        "run_shell", {"command": "git status"}, ask=ask
    )
    assert decision == "deny"
    assert "rate limit" in reason


@pytest.mark.asyncio
async def test_check_with_reason_names_missing_ui_when_ask_is_none(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    decision, reason = await manager.check_with_reason(
        "run_shell", {"command": "git status"}, ask=None
    )
    assert decision == "deny"
    assert "no UI available" in reason


@pytest.mark.asyncio
async def test_check_with_reason_distinguishes_a_user_denial_from_a_guardrail_denial(
    tmp_path: Path,
):
    manager = PermissionManager(
        guardrails=_guardrails(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )

    async def ask(tool_name, arguments, risk_description):
        return ("deny", None)

    decision, reason = await manager.check_with_reason(
        "run_shell", {"command": "git status"}, ask=ask
    )
    assert decision == "deny"
    assert reason == "denied by the user"


@pytest.mark.asyncio
async def test_check_with_reason_names_a_remembered_always_deny(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    manager.policy.remember("run_shell", scope="always", decision="deny")

    async def ask_should_not_be_called(tool_name, arguments, risk_description):
        raise AssertionError("ask() should not be called once a grant is remembered")

    decision, reason = await manager.check_with_reason(
        "run_shell", {"command": "git status"}, ask=ask_should_not_be_called
    )
    assert decision == "deny"
    assert reason == "previously denied and remembered"


@pytest.mark.asyncio
async def test_check_with_reason_has_no_reason_for_a_plain_allow(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    decision, reason = await manager.check_with_reason(
        "read_file", {"path": "/allowed/x.txt"}, path="/allowed/x.txt", default_allow=True
    )
    assert decision == "allow"
    assert reason is None


@pytest.mark.asyncio
async def test_check_still_returns_a_plain_decision_without_reason(tmp_path: Path):
    """check() itself must be unaffected by the check_with_reason() refactor
    - it's still the plain two-value API every other existing call site
    (and dozens of tests above) rely on."""
    manager = PermissionManager(
        guardrails=_guardrails(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    decision = await manager.check(
        "run_shell", {"command": "rm -rf /"}, command="rm -rf /", ask=None
    )
    assert decision == "deny"


# --- audit_enabled: permission_decision AuditEntry recording ---
#
# audit_enabled is off by default and must produce zero entries regardless
# of outcome - these tests all pass audit_enabled=True explicitly, plus one
# test confirming the opposite (audit_enabled=False records nothing).


@pytest.mark.asyncio
async def test_audit_disabled_by_default_records_nothing(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(), policy=PermissionPolicy(persist_path=tmp_path / "p.json")
    )
    session = Session()

    async def ask(tool_name, arguments, risk_description):
        return ("deny", None)

    await manager.check_with_reason("run_shell", {"command": "git status"}, ask=ask, session=session)
    assert session.audit_log == []


@pytest.mark.asyncio
async def test_audit_records_an_interactive_allow(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(),
        policy=PermissionPolicy(persist_path=tmp_path / "p.json"),
        audit_enabled=True,
    )
    session = Session()

    async def ask(tool_name, arguments, risk_description):
        return ("allow", "once")

    await manager.check_with_reason(
        "run_shell", {"command": "git status"}, ask=ask, session=session,
        risk_description="runs a shell command",
    )

    assert len(session.audit_log) == 1
    entry = session.audit_log[0]
    assert entry.kind == "permission_decision"
    assert entry.detail["tool_name"] == "run_shell"
    assert entry.detail["decision"] == "allow"
    assert entry.detail["mechanism"] == "interactive"
    assert entry.detail["risk_description"] == "runs a shell command"


@pytest.mark.asyncio
async def test_audit_records_an_interactive_deny(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(),
        policy=PermissionPolicy(persist_path=tmp_path / "p.json"),
        audit_enabled=True,
    )
    session = Session()

    async def ask(tool_name, arguments, risk_description):
        return ("deny", None)

    await manager.check_with_reason(
        "run_shell", {"command": "git status"}, ask=ask, session=session
    )

    assert len(session.audit_log) == 1
    assert session.audit_log[0].detail["decision"] == "deny"
    assert session.audit_log[0].detail["mechanism"] == "interactive"


@pytest.mark.asyncio
async def test_audit_records_a_guardrail_command_denial(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(),
        policy=PermissionPolicy(persist_path=tmp_path / "p.json"),
        audit_enabled=True,
    )
    session = Session()

    await manager.check_with_reason(
        "run_shell", {"command": "rm -rf /"}, command="rm -rf /", ask=None, session=session
    )

    assert len(session.audit_log) == 1
    entry = session.audit_log[0]
    assert entry.detail["decision"] == "deny"
    assert entry.detail["mechanism"] == "guardrail_command"
    assert "rm -rf /" in entry.detail["reason"]


@pytest.mark.asyncio
async def test_audit_records_a_guardrail_path_denial(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(),
        policy=PermissionPolicy(persist_path=tmp_path / "p.json"),
        audit_enabled=True,
    )
    session = Session()

    await manager.check_with_reason(
        "write_file", {"path": "/somewhere/else/file.txt"},
        path="/somewhere/else/file.txt", ask=None, session=session,
    )

    assert session.audit_log[0].detail["mechanism"] == "guardrail_path"


@pytest.mark.asyncio
async def test_audit_records_a_rate_limit_denial(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(max_tool_calls_per_minute=1),
        policy=PermissionPolicy(persist_path=tmp_path / "p.json"),
        audit_enabled=True,
    )
    session = Session()

    async def ask(tool_name, arguments, risk_description):
        return ("allow", "once")

    await manager.check_with_reason(
        "run_shell", {"command": "git status"}, ask=ask, session=session
    )
    await manager.check_with_reason(
        "run_shell", {"command": "git status"}, ask=ask, session=session
    )

    assert len(session.audit_log) == 2  # first allow, then the rate-limited deny
    assert session.audit_log[1].detail["mechanism"] == "rate_limit"
    assert session.audit_log[1].detail["decision"] == "deny"


@pytest.mark.asyncio
async def test_audit_records_a_remembered_policy_hit(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(),
        policy=PermissionPolicy(persist_path=tmp_path / "p.json"),
        audit_enabled=True,
    )
    manager.policy.remember("run_shell", scope="always", decision="allow")
    session = Session()

    async def ask_should_not_be_called(tool_name, arguments, risk_description):
        raise AssertionError("ask() should not be called once a grant is remembered")

    await manager.check_with_reason(
        "run_shell", {"command": "git status"}, ask=ask_should_not_be_called, session=session
    )

    assert len(session.audit_log) == 1
    assert session.audit_log[0].detail["mechanism"] == "remembered_policy"


@pytest.mark.asyncio
async def test_audit_records_a_headless_fail_closed_denial(tmp_path: Path):
    manager = PermissionManager(
        guardrails=_guardrails(),
        policy=PermissionPolicy(persist_path=tmp_path / "p.json"),
        audit_enabled=True,
    )
    session = Session()

    await manager.check_with_reason(
        "run_shell", {"command": "git status"}, ask=None, session=session
    )

    assert session.audit_log[0].detail["mechanism"] == "no_ui_fail_closed"


@pytest.mark.asyncio
async def test_audit_skips_a_default_allow_tool_with_no_guardrail_hit(tmp_path: Path):
    """The one deliberate no-op case: a tool that doesn't need permission
    and hit no guardrail decided nothing worth auditing - recording it
    would be noise (every single read_file call, the most common tool
    call in any session)."""
    manager = PermissionManager(
        guardrails=_guardrails(),
        policy=PermissionPolicy(persist_path=tmp_path / "p.json"),
        audit_enabled=True,
    )
    session = Session()

    await manager.check_with_reason(
        "read_file", {"path": "/allowed/x.txt"}, path="/allowed/x.txt",
        default_allow=True, session=session,
    )

    assert session.audit_log == []


@pytest.mark.asyncio
async def test_audit_still_records_a_guardrail_denial_on_a_default_allow_tool(tmp_path: Path):
    """A default_allow tool (e.g. read_file) is still gated by guardrails -
    a guardrail denial on one of these must still be recorded, unlike the
    no-guardrail-hit case above."""
    manager = PermissionManager(
        guardrails=_guardrails(),
        policy=PermissionPolicy(persist_path=tmp_path / "p.json"),
        audit_enabled=True,
    )
    session = Session()

    await manager.check_with_reason(
        "read_file", {"path": "/somewhere/else/file.txt"},
        path="/somewhere/else/file.txt", default_allow=True, session=session,
    )

    assert len(session.audit_log) == 1
    assert session.audit_log[0].detail["mechanism"] == "guardrail_path"
    assert session.audit_log[0].detail["decision"] == "deny"


@pytest.mark.asyncio
async def test_audit_enabled_with_no_session_records_nothing(tmp_path: Path):
    """audit_enabled=True but no session passed - nothing to append to, no
    crash either (mirrors how PermissionGrant recording already behaves)."""
    manager = PermissionManager(
        guardrails=_guardrails(),
        policy=PermissionPolicy(persist_path=tmp_path / "p.json"),
        audit_enabled=True,
    )

    decision = await manager.check(
        "run_shell", {"command": "rm -rf /"}, command="rm -rf /", ask=None
    )
    assert decision == "deny"  # just confirming no crash with session=None
