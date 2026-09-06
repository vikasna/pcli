"""Coverage for download_file: a cross-platform, shell-free replacement for
curl/wget/Invoke-WebRequest, built on httpx (already a pcli dependency)."""

from pathlib import Path

import httpx
import pytest
import respx

import pcli.tools.builtin.network_tools as network_tools_module
from pcli.permissions.guardrails import GuardrailsConfig
from pcli.sandbox.base import ExecRequest, ExecResult, Sandbox, SandboxCapabilities
from pcli.tools.base import ToolContext
from pcli.tools.builtin.network_tools import DOWNLOAD_FILE


class _NullSandbox(Sandbox):
    name = "null"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(False, False, False)

    async def execute(self, request: ExecRequest) -> ExecResult:
        raise AssertionError("download_file should never touch the sandbox")


def _ctx(cwd: Path) -> ToolContext:
    return ToolContext(sandbox=_NullSandbox(), guardrails=GuardrailsConfig(), cwd=cwd)


@pytest.mark.asyncio
@respx.mock
async def test_download_file_saves_content_and_reports_byte_count(tmp_path: Path):
    respx.get("https://example.test/data.csv").mock(
        return_value=httpx.Response(200, content=b"a,b,c\n1,2,3\n")
    )

    result = await DOWNLOAD_FILE.handler(
        {"url": "https://example.test/data.csv", "path": "data.csv"}, _ctx(tmp_path)
    )

    assert result.is_error is False
    assert "12" in result.output  # byte count
    saved = tmp_path / "data.csv"
    assert saved.read_bytes() == b"a,b,c\n1,2,3\n"


@pytest.mark.asyncio
@respx.mock
async def test_download_file_creates_parent_directories(tmp_path: Path):
    respx.get("https://example.test/file.bin").mock(return_value=httpx.Response(200, content=b"x"))

    result = await DOWNLOAD_FILE.handler(
        {"url": "https://example.test/file.bin", "path": "nested/dir/file.bin"}, _ctx(tmp_path)
    )

    assert result.is_error is False
    assert (tmp_path / "nested" / "dir" / "file.bin").exists()


@pytest.mark.asyncio
@respx.mock
async def test_download_file_follows_redirects(tmp_path: Path):
    respx.get("https://example.test/old-location").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.test/new-location"})
    )
    respx.get("https://example.test/new-location").mock(
        return_value=httpx.Response(200, content=b"redirected content")
    )

    result = await DOWNLOAD_FILE.handler(
        {"url": "https://example.test/old-location", "path": "out.bin"}, _ctx(tmp_path)
    )

    assert result.is_error is False
    assert (tmp_path / "out.bin").read_bytes() == b"redirected content"


@pytest.mark.asyncio
@respx.mock
async def test_download_file_reports_http_error_status_with_a_suggestion(tmp_path: Path):
    respx.get("https://example.test/missing.csv").mock(return_value=httpx.Response(404))

    result = await DOWNLOAD_FILE.handler(
        {"url": "https://example.test/missing.csv", "path": "missing.csv"}, _ctx(tmp_path)
    )

    assert result.is_error is True
    assert "404" in result.output
    assert "[pcli] Suggestion:" in result.output
    assert not (tmp_path / "missing.csv").exists()  # never created on an upfront HTTP error


@pytest.mark.asyncio
@respx.mock
async def test_download_file_removes_partial_content_on_timeout(tmp_path: Path):
    respx.get("https://example.test/slow.bin").mock(side_effect=httpx.ReadTimeout("too slow"))

    result = await DOWNLOAD_FILE.handler(
        {"url": "https://example.test/slow.bin", "path": "slow.bin", "timeout_s": 1}, _ctx(tmp_path)
    )

    assert result.is_error is True
    assert "timed out" in result.output
    assert "[pcli] Suggestion:" in result.output
    assert not (tmp_path / "slow.bin").exists()


@pytest.mark.asyncio
@respx.mock
async def test_download_file_removes_partial_content_on_connection_error(tmp_path: Path):
    respx.get("https://example.test/unreachable.bin").mock(side_effect=httpx.ConnectError("refused"))

    result = await DOWNLOAD_FILE.handler(
        {"url": "https://example.test/unreachable.bin", "path": "unreachable.bin"}, _ctx(tmp_path)
    )

    assert result.is_error is True
    assert "Download failed" in result.output
    assert not (tmp_path / "unreachable.bin").exists()


@pytest.mark.asyncio
@respx.mock
async def test_download_file_aborts_and_cleans_up_when_exceeding_the_size_cap(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(network_tools_module, "_MAX_DOWNLOAD_BYTES", 10)
    respx.get("https://example.test/huge.bin").mock(
        return_value=httpx.Response(200, content=b"x" * 11)
    )

    result = await DOWNLOAD_FILE.handler(
        {"url": "https://example.test/huge.bin", "path": "huge.bin"}, _ctx(tmp_path)
    )

    assert result.is_error is True
    assert "safety cap" in result.output
    assert not (tmp_path / "huge.bin").exists()


def test_download_file_needs_permission_and_has_a_risk_description():
    assert DOWNLOAD_FILE.needs_permission is True
    assert DOWNLOAD_FILE.risk_description


def test_download_file_guards_the_destination_path():
    assert DOWNLOAD_FILE.guardrail_path_arg == "path"
