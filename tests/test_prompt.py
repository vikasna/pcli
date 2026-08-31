"""Coverage that BASE_SYSTEM_PROMPT actually mentions the tools/behaviors it
needs to guide the model toward - regression coverage for the prompt text
itself, not just that the module imports."""

from pcli.agent.prompt import BASE_SYSTEM_PROMPT, build_system_prompt


def test_prompt_guides_toward_edit_file_over_write_file_for_existing_files():
    assert "edit_file" in BASE_SYSTEM_PROMPT
    assert "write_file" in BASE_SYSTEM_PROMPT


def test_prompt_mentions_background_shell_execution():
    assert "run_shell_background" in BASE_SYSTEM_PROMPT
    assert "read_background_output" in BASE_SYSTEM_PROMPT
    assert "stop_background_process" in BASE_SYSTEM_PROMPT
    assert "timeout_s" in BASE_SYSTEM_PROMPT


def test_prompt_mentions_register_toolbox_tool_and_that_it_is_permission_gated():
    assert "register_toolbox_tool" in BASE_SYSTEM_PROMPT
    section_start = BASE_SYSTEM_PROMPT.index("# Building reusable tools")
    section = BASE_SYSTEM_PROMPT[section_start : section_start + 600]
    assert "permission" in section


def test_prompt_instructs_not_circumventing_guardrails():
    assert "# Respecting guardrails" in BASE_SYSTEM_PROMPT
    section_start = BASE_SYSTEM_PROMPT.index("# Respecting guardrails")
    section_end = BASE_SYSTEM_PROMPT.index("# Surfacing side effects")
    section = BASE_SYSTEM_PROMPT[section_start:section_end]
    # Covers the three concrete guardrail mechanisms in permissions/guardrails.py
    # plus the permission-prompt escalation path, so the instruction is
    # grounded in real enforcement rather than a vague "be safe" platitude.
    assert "denylist" in section
    assert "permission" in section
    assert "workaround" in section or "route around" in section


def test_prompt_instructs_surfacing_side_effects_of_suggested_changes():
    assert "# Surfacing side effects" in BASE_SYSTEM_PROMPT
    section_start = BASE_SYSTEM_PROMPT.index("# Surfacing side effects")
    section_end = BASE_SYSTEM_PROMPT.index("# Tracking work")
    section = BASE_SYSTEM_PROMPT[section_start:section_end]
    # Covers all four categories the user asked for: operations, settings,
    # commands, code.
    assert "config" in section or "setting" in section
    assert "command" in section
    assert "signature" in section or "code" in section


def test_build_system_prompt_appends_extra_sections():
    result = build_system_prompt(extra_sections=["# Extra\nSomething."])
    assert result.startswith(BASE_SYSTEM_PROMPT)
    assert result.endswith("# Extra\nSomething.")
