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


def test_prompt_managing_context_mentions_the_pattern_parameter():
    section_start = BASE_SYSTEM_PROMPT.index("# Managing context")
    section_end = BASE_SYSTEM_PROMPT.index("# Investigation scripts")
    section = BASE_SYSTEM_PROMPT[section_start:section_end]
    assert "pattern" in section
    assert "context_lines" in section


def test_prompt_directs_broad_exploration_to_spawn_subagent_for_context():
    section_start = BASE_SYSTEM_PROMPT.index("# Managing context")
    section_end = BASE_SYSTEM_PROMPT.index("# Investigation scripts")
    section = BASE_SYSTEM_PROMPT[section_start:section_end]
    assert "spawn_subagent" in section
    assert "self-contained task description" in section


def test_prompt_instructs_planning_investigation_scripts_and_narrow_output():
    assert "# Investigation scripts" in BASE_SYSTEM_PROMPT
    section_start = BASE_SYSTEM_PROMPT.index("# Investigation scripts")
    section_end = BASE_SYSTEM_PROMPT.index("# Long-running and background commands")
    section = BASE_SYSTEM_PROMPT[section_start:section_end]
    # Generalized guidance, not Kubernetes-specific - pcli is a domain-generic
    # coding agent, so the base prompt shouldn't bake in one domain's tooling.
    assert "kubernetes" not in section.lower()
    assert "k8s" not in section.lower()
    assert "one focused script per question" in section
    assert "truncation" in section


def test_prompt_instructs_resuming_from_a_short_continue_style_message():
    assert "# Resuming after a break" in BASE_SYSTEM_PROMPT
    section_start = BASE_SYSTEM_PROMPT.index("# Resuming after a break")
    section_end = BASE_SYSTEM_PROMPT.index("# Recording decisions")
    section = BASE_SYSTEM_PROMPT[section_start:section_end]
    # Covers a few real phrasings, not just the literal word "continue" -
    # regression coverage for a real gap: a bare "continue" after reopening
    # a session was previously ambiguous to the model.
    assert '"continue"' in section
    assert '"keep going"' in section or '"resume"' in section
    assert "todo list" in section
    assert "ask" in section  # tells it when clarification IS still warranted


def test_prompt_instructs_diagnosing_before_retrying_a_failed_tool_call():
    assert "# Recovering from a failed tool call" in BASE_SYSTEM_PROMPT
    section_start = BASE_SYSTEM_PROMPT.index("# Recovering from a failed tool call")
    section_end = BASE_SYSTEM_PROMPT.index("# Building reusable tools")
    section = BASE_SYSTEM_PROMPT[section_start:section_end]
    # Grounded in a real debugged session: the model repeatedly retried a
    # bash heredoc that fails on Windows (cosmetic delimiter-name changes
    # only), then re-hit a dead download URL, without ever diagnosing why
    # either kept failing. Covers all three named failure categories plus
    # the explicit retry cap, not just a vague "don't repeat mistakes".
    assert "404" in section or "external fact" in section
    assert "heredoc" in section or "shell you're actually running" in section
    assert "write_file" in section
    assert "precondition" in section
    assert "two consecutive attempts" in section


def test_prompt_guides_toward_download_file_over_shell_downloads():
    assert "# Downloading files" in BASE_SYSTEM_PROMPT
    section_start = BASE_SYSTEM_PROMPT.index("# Downloading files")
    section_end = BASE_SYSTEM_PROMPT.index("# Building reusable tools")
    section = BASE_SYSTEM_PROMPT[section_start:section_end]
    assert "download_file" in section
    assert "curl" in section
    assert "wget" in section


def test_build_system_prompt_appends_extra_sections():
    result = build_system_prompt(extra_sections=["# Extra\nSomething."])
    assert BASE_SYSTEM_PROMPT in result
    assert result.endswith("# Extra\nSomething.")


def test_build_system_prompt_leads_with_a_real_environment_fact():
    """Regression coverage for a real repeated failure mode: with no actual
    fact to go on, the model defaulted to assuming Linux/bash (a heredoc
    failing with a cmd.exe syntax error, a write_file call denied for
    targeting /tmp on a Windows host) - the environment section is now
    computed fresh and placed before BASE_SYSTEM_PROMPT so it's ground
    truth from the very first turn, not something discovered by failing."""
    import platform

    result = build_system_prompt()
    assert result.startswith("# Environment")
    assert result.index("# Environment") < result.index(BASE_SYSTEM_PROMPT)
    system = platform.system()
    if system == "Windows":
        assert "cmd.exe" in result
        assert "heredoc" in result
    elif system in ("Linux", "Darwin"):
        assert f"running on {system}" in result
    assert "Docker" in result  # the sandbox caveat is present regardless of host OS
