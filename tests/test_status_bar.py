from pcli.tui.widgets.status_bar import format_model_name


def test_format_model_name_shortens_a_windows_gguf_path_to_the_bare_model_name():
    """Regression coverage for a real gap: a raw llama-server instance with
    no --alias configured reports its own "model" id as the full loaded
    GGUF path, unlike LM Studio's short friendly names - the status bar
    line has no room for that and other fields end up pushed off-screen."""
    path = (
        r"E:\.lmstudio\models\lmstudio-community\Mistral-Nemo-Instruct-2407-GGUF"
        r"\Mistral-Nemo-Instruct-2407-Q4_K_M.gguf"
    )
    assert format_model_name(path) == "Mistral-Nemo-Instruct-2407-Q4_K_M"


def test_format_model_name_shortens_a_posix_gguf_path():
    path = "/home/user/models/some-directory/Model-Name-Q4.gguf"
    assert format_model_name(path) == "Model-Name-Q4"


def test_format_model_name_leaves_a_short_friendly_id_unchanged():
    assert format_model_name("qwen2.5-coder-7b-instruct") == "qwen2.5-coder-7b-instruct"


def test_format_model_name_truncates_a_long_non_path_name_as_a_fallback():
    name = "a-plain-name-with-no-slashes-that-is-really-quite-long-indeed-here"
    result = format_model_name(name)
    assert len(result) <= 40
    assert result.endswith("…")


def test_format_model_name_handles_empty_string():
    assert format_model_name("") == ""
