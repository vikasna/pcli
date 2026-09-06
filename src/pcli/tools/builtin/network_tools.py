"""Pythonic network tools — structured, cross-platform alternatives to
shelling out to curl/wget/Invoke-WebRequest, which differ across shells and
platforms and aren't uniformly installed (confirmed via a real debugged
session: a model repeatedly retrying a bash heredoc that fails outright on
a Windows/cmd.exe shell). Built on httpx (already a pcli dependency, used
throughout llm/client.py), not requests, to avoid adding a new one.
"""

from __future__ import annotations

import httpx

from pcli.tools.base import ToolContext, ToolResult, ToolSpec
from pcli.tools.builtin.fs_tools import resolve_path

_DEFAULT_DOWNLOAD_TIMEOUT_S = 30.0
_MAX_DOWNLOAD_BYTES = 500_000_000  # 500 MB safety cap, mirrors other tools' internal size caps


class _DownloadTooLarge(Exception):
    pass


async def _download_file(arguments: dict, ctx: ToolContext) -> ToolResult:
    url = arguments["url"]
    resolved = resolve_path(arguments["path"], ctx)
    timeout_s = arguments.get("timeout_s", _DEFAULT_DOWNLOAD_TIMEOUT_S)
    resolved.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    try:
        async with (
            httpx.AsyncClient(follow_redirects=True, timeout=timeout_s) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code >= 400:
                return ToolResult(
                    output=f"Download failed: HTTP {response.status_code} for {url}\n"
                    "[pcli] Suggestion: double-check the URL is correct and still reachable "
                    "(e.g. a dataset/release URL may have moved or been renamed) — retrying "
                    "the identical URL won't fix a 404/403/etc., re-verify the source first.",
                    is_error=True,
                )
            with resolved.open("wb") as f:
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_DOWNLOAD_BYTES:
                        raise _DownloadTooLarge
                    f.write(chunk)
    except _DownloadTooLarge:
        resolved.unlink(missing_ok=True)
        return ToolResult(
            output=f"Download exceeded the {_MAX_DOWNLOAD_BYTES:,}-byte safety cap and was "
            "aborted; the partial file was removed.\n"
            "[pcli] Suggestion: if this file is genuinely expected to be this large, use "
            "run_shell_background with a dedicated download command instead.",
            is_error=True,
        )
    except httpx.TimeoutException:
        resolved.unlink(missing_ok=True)
        return ToolResult(
            output=f"Download timed out after {timeout_s:g}s for {url}\n"
            "[pcli] Suggestion: raise timeout_s for a large file or slow connection, or use "
            "run_shell_background if it may take several minutes.",
            is_error=True,
        )
    except httpx.HTTPError as exc:
        resolved.unlink(missing_ok=True)
        return ToolResult(
            output=f"Download failed: {exc}\n"
            "[pcli] Suggestion: check the URL is reachable and correctly formed, and that "
            "network access is actually available from this environment.",
            is_error=True,
        )

    return ToolResult(output=f"Downloaded {total:,} bytes from {url} to {resolved}")


DOWNLOAD_FILE = ToolSpec(
    name="download_file",
    description="Downloads a file from a URL and saves it to a local path. Prefer this over "
    "run_shell with curl/wget/Invoke-WebRequest: it's one cross-platform tool call with no "
    "shell syntax to get wrong, and reports a clear HTTP status/error instead of a raw stderr "
    "blob you'd have to parse yourself. Follows redirects automatically.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to download from."},
            "path": {
                "type": "string",
                "description": "Destination path to save the downloaded file to, relative to "
                "the working directory or absolute. Parent directories are created as needed.",
            },
            "timeout_s": {
                "type": "number",
                "description": f"Timeout in seconds for the download (default "
                f"{_DEFAULT_DOWNLOAD_TIMEOUT_S:g}).",
            },
        },
        "required": ["url", "path"],
    },
    handler=_download_file,
    needs_permission=True,
    risk_description="Downloads content from a URL and writes it to disk.",
    guardrail_path_arg="path",
)
