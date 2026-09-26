"""Coverage that BASE_SYSTEM_PROMPT actually mentions the tools/behaviors it
needs to guide the model toward - regression coverage for the prompt text
itself, not just that the module imports."""

from pcli.agent.prompt import BASE_SYSTEM_PROMPT, build_system_prompt


def test_prompt_guides_toward_edit_file_over_write_file_for_existing_files():
    assert "edit_file" in BASE_SYSTEM_PROMPT
    assert "write_file" in BASE_SYSTEM_PROMPT


def test_prompt_mentions_diff_files_and_apply_patch():
    section_start = BASE_SYSTEM_PROMPT.index("# Editing files")
    section_end = BASE_SYSTEM_PROMPT.index("# Executing actions with care")
    section = BASE_SYSTEM_PROMPT[section_start:section_end]
    assert "diff_files" in section
    assert "apply_patch" in section


def test_prompt_introduces_describe_tool_and_points_to_it_from_edit_file():
    """describe_tool needs to be established as a general practice (its own
    section) *and* concretely pointed at from the one real case that
    prompted it (edit_file's multiple modes) - a tool the model doesn't
    know exists can't help it."""
    assert "# Tool documentation" in BASE_SYSTEM_PROMPT
    section_start = BASE_SYSTEM_PROMPT.index("# Tool documentation")
    section_end = BASE_SYSTEM_PROMPT.index("# Editing files")
    section = BASE_SYSTEM_PROMPT[section_start:section_end]
    assert "describe_tool" in section

    edit_files_start = BASE_SYSTEM_PROMPT.index("# Editing files")
    edit_files_end = BASE_SYSTEM_PROMPT.index("# Executing actions with care")
    edit_files_section = BASE_SYSTEM_PROMPT[edit_files_start:edit_files_end]
    assert "describe_tool" in edit_files_section


def test_prompt_explains_edit_files_three_modes_and_warns_against_faking_an_insert():
    """Regression coverage for a real reported confusion: some models pass
    the same anchor text as both old_string and new_string when they
    actually want to insert new content, since the tool/prompt never
    described what old_string means or how to insert without replacing.
    edit_file itself now rejects old_string == new_string outright (see
    test_builtin_tools.py), but the prompt should also steer the model
    toward the right tool call in the first place, not just react to the
    mistake after it's made."""
    section_start = BASE_SYSTEM_PROMPT.index("# Editing files")
    section_end = BASE_SYSTEM_PROMPT.index("# Executing actions with care")
    section = BASE_SYSTEM_PROMPT[section_start:section_end]
    assert "verbatim" in section  # old_string must reflect real existing content
    assert "insert_after_line" in section
    assert "delete_start_line" in section
    assert "delete_end_line" in section
    assert "replace_all" in section
    assert "identical" in section  # the old_string == new_string footgun, named explicitly


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


def test_prompt_instructs_surfacing_assumptions_and_limiting_scope():
    section_start = BASE_SYSTEM_PROMPT.index("# Doing tasks")
    section_end = BASE_SYSTEM_PROMPT.index("# Verifying your own work")
    section = BASE_SYSTEM_PROMPT[section_start:section_end]
    assert "say so plainly" in section
    assert "clean up" in section


def test_prompt_instructs_when_and_when_not_to_use_ask_user_question():
    assert "# Asking questions" in BASE_SYSTEM_PROMPT
    section_start = BASE_SYSTEM_PROMPT.index("# Asking questions")
    section_end = BASE_SYSTEM_PROMPT.index("# Verifying your own work")
    section = BASE_SYSTEM_PROMPT[section_start:section_end]
    assert "ask_user_question" in section
    assert "not a first resort" in section
    assert "options" in section


def test_prompt_instructs_verifying_work_before_declaring_done():
    assert "# Verifying your own work" in BASE_SYSTEM_PROMPT
    section_start = BASE_SYSTEM_PROMPT.index("# Verifying your own work")
    section_end = BASE_SYSTEM_PROMPT.index("# Editing files")
    section = BASE_SYSTEM_PROMPT[section_start:section_end]
    assert "obviously-correct" in section
    assert "tests/build/lint" in section


def test_prompt_instructs_verifying_a_subagents_claims_before_relaying_them():
    """Regression coverage for a real observed session: a subagent made a
    single tool call, reported a large multi-file task as fully complete,
    and the parent relayed that fabricated summary to the user with no
    verification of its own."""
    section_start = BASE_SYSTEM_PROMPT.index("# Verifying your own work")
    section_end = BASE_SYSTEM_PROMPT.index("# Editing files")
    section = BASE_SYSTEM_PROMPT[section_start:section_end]
    assert "subagent" in section
    assert "claim" in section
    assert "tool-call count" in section


def test_prompt_instructs_outcome_based_todo_items():
    section_start = BASE_SYSTEM_PROMPT.index("# Tracking work")
    section_end = BASE_SYSTEM_PROMPT.index("# Resuming after a break")
    section = BASE_SYSTEM_PROMPT[section_start:section_end]
    assert "checkable outcome" in section


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


def test_prompt_instructs_batching_independent_tool_calls_into_one_response():
    section_start = BASE_SYSTEM_PROMPT.index("# Managing context")
    section_end = BASE_SYSTEM_PROMPT.index("# Investigation scripts")
    section = BASE_SYSTEM_PROMPT[section_start:section_end]
    assert "request all of those tool calls together" in section
    assert "genuinely independent" in section


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
    # The retry cap is mechanically enforced (AgentLoop._dispatch_tool_call's
    # identical-call guard), not just advisory - the model should know a
    # third identical attempt will actually be refused, not merely
    # discouraged.
    assert "mechanically blocked" in section


def test_prompt_guides_toward_network_tools_over_shell_downloads():
    assert "# Network access" in BASE_SYSTEM_PROMPT
    section_start = BASE_SYSTEM_PROMPT.index("# Network access")
    section_end = BASE_SYSTEM_PROMPT.index("# Building reusable tools")
    section = BASE_SYSTEM_PROMPT[section_start:section_end]
    assert "download_file" in section
    assert "web_fetch" in section
    assert "web_search" in section
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


def test_environment_section_instructs_preferring_relative_paths():
    """A relative path resolves against the actual known working directory
    (see tools/builtin/fs_tools.py's path resolution); a model-guessed
    absolute path, especially in the wrong OS's convention, is far more
    likely to be wrong - this must hold regardless of host OS, so it's part
    of environment_section() rather than only the Windows-specific branch."""
    from pcli.agent.prompt import environment_section

    text = environment_section()
    assert "relative to the working directory" in text
    assert "unless the user gave you an explicit absolute path" in text


def test_environment_section_is_reused_verbatim_by_nested_agents():
    """Regression coverage for a real gap: a subagent's system prompt fully
    replaces the main loop's rather than extending it, so it previously got
    no OS/shell/path facts at all and independently defaulted to the same
    Linux/bash assumption the main loop's own environment section exists to
    prevent. tools/_nested_agent.py imports and reuses this exact function."""
    import pcli.tools._nested_agent as nested_agent_module
    from pcli.agent.prompt import environment_section

    assert nested_agent_module.environment_section is environment_section
