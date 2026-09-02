"""Coverage for update_guardrails_limits: persists [limits] keys into
guardrails.toml while preserving the [shell]/[fs]/[python] tables, and
correctly treats 0 as a meaningful value (unlimited) rather than skipping
it the way a plain truthy check would."""

from pcli.permissions.guardrails import GuardrailsConfig, guardrails_file, update_guardrails_limits


def test_update_guardrails_limits_creates_the_file_if_missing():
    assert not guardrails_file().exists()

    update_guardrails_limits(max_tool_calls_per_turn=50)

    assert guardrails_file().exists()
    cfg = GuardrailsConfig.load()
    assert cfg.max_tool_calls_per_turn == 50


def test_update_guardrails_limits_preserves_other_tables():
    GuardrailsConfig.load()  # materializes the default file first
    original = GuardrailsConfig.load()

    update_guardrails_limits(max_tool_calls_per_turn=50)

    reloaded = GuardrailsConfig.load()
    assert reloaded.max_tool_calls_per_turn == 50
    assert reloaded.shell_denylist == original.shell_denylist
    assert reloaded.fs_allowed_roots == original.fs_allowed_roots
    assert reloaded.fs_deny_paths == original.fs_deny_paths
    assert reloaded.python_module_denylist == original.python_module_denylist


def test_update_guardrails_limits_preserves_other_limits_keys():
    GuardrailsConfig.load()
    update_guardrails_limits(max_tool_calls_per_turn=50)

    update_guardrails_limits(max_tool_calls_per_minute=10)

    cfg = GuardrailsConfig.load()
    assert cfg.max_tool_calls_per_turn == 50  # untouched by the second call
    assert cfg.max_tool_calls_per_minute == 10


def test_update_guardrails_limits_accepts_zero_as_a_real_value():
    """0 means unlimited (see AgentLoop.run_turn / PermissionManager's rate
    check) - a plain truthy-skip (like update_config_file's) would silently
    drop this, so update_guardrails_limits must check `is not None` instead."""
    update_guardrails_limits(max_tool_calls_per_turn=50)
    update_guardrails_limits(max_tool_calls_per_turn=0)

    cfg = GuardrailsConfig.load()
    assert cfg.max_tool_calls_per_turn == 0
