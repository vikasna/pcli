"""ask_via_telegram: PermissionManager's `ask` callback (permissions/
manager.py's AskCallback), implemented over Telegram instead of a Textual
modal (tui/screens/permission_modal.py's ask_via_modal is the TUI
equivalent - same signature, same PermissionModalResult return shape).

Deliberately has no dependency on the `telegram`/python-telegram-bot
package at all - it only talks to a small TelegramSender protocol (send a
message, optionally with inline buttons). The real PTB-backed sender lives
in telegram/sender.py, which is the one place that actually imports PTB
(lazily, same discipline as browser/session.py's Playwright import - see
that module's docstring for why). That split is what makes this module,
and everything it does, testable without PTB installed at all.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any, Protocol

from pcli.permissions.manager import PermissionDecision, RememberScope

PermissionModalResult = tuple[PermissionDecision, RememberScope | None]

_BUTTONS: list[tuple[str, PermissionModalResult]] = [
    ("Allow Once", ("allow", "once")),
    ("Allow for Session", ("allow", "session")),
    ("Allow Always", ("allow", "always")),
    ("Deny", ("deny", None)),
]

# Tool arguments can carry arbitrarily long content (e.g. write_file's full
# text) - same truncation precedent as permission_modal.py's own
# _MAX_ARG_PREVIEW_CHARS, so a Telegram message doesn't blow past Telegram's
# own 4096-char message limit.
_MAX_ARG_PREVIEW_CHARS = 800


class TelegramSender(Protocol):
    async def send_message(
        self, chat_id: int, text: str, *, buttons: list[tuple[str, str]] | None = None
    ) -> None:
        """buttons, if given, is a flat list of (label, callback_data) pairs,
        one inline button per row - the real sender (telegram/sender.py)
        turns this into an InlineKeyboardMarkup; this protocol says nothing
        about PTB's own types, on purpose."""
        ...

    async def send_photo(self, chat_id: int, path: Any) -> None: ...


class PendingApprovals:
    """In-memory correlation between an outgoing permission prompt and the
    inline-button press that eventually answers it. Telegram updates arrive
    asynchronously with no built-in request/response pairing - each prompt
    gets a short request id embedded in every button's callback_data, and
    the daemon's callback-query handler resolves the matching future when a
    press comes back. Never persisted: a request outstanding when the
    daemon restarts is simply lost (the same as a TUI permission modal open
    when the app is killed - there's no session to resume it into)."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[PermissionModalResult]] = {}

    def register(self) -> tuple[str, asyncio.Future[PermissionModalResult]]:
        request_id = secrets.token_hex(4)
        future: asyncio.Future[PermissionModalResult] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        return request_id, future

    def resolve(self, request_id: str, result: PermissionModalResult) -> bool:
        """False if request_id is unknown or already resolved (a stale
        button press after a restart, or a double-tap) - callers should
        treat that as a harmless no-op, not an error."""
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return False
        future.set_result(result)
        return True


def _format_prompt(tool_name: str, arguments: dict[str, Any], risk_description: str) -> str:
    args_text = str(arguments)
    if len(args_text) > _MAX_ARG_PREVIEW_CHARS:
        omitted = len(args_text) - _MAX_ARG_PREVIEW_CHARS
        args_text = f"{args_text[:_MAX_ARG_PREVIEW_CHARS]}\n... [{omitted} more chars truncated]"
    lines = [f"Tool wants to run: {tool_name}", args_text]
    if risk_description:
        lines.append(risk_description)
    return "\n\n".join(lines)


def _encode_callback_data(request_id: str, result: PermissionModalResult) -> str:
    decision, scope = result
    # Telegram caps callback_data at 64 bytes - "perm:<8 hex>:allow:always"
    # is comfortably under that regardless of which button this is.
    return f"perm:{request_id}:{decision}:{scope or '-'}"


def decode_callback_data(data: str) -> tuple[str, PermissionModalResult] | None:
    """None if data isn't one of ours (e.g. a future, unrelated bot
    feature's callback_data) - the daemon's callback-query handler treats
    that as a no-op rather than an error."""
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "perm":
        return None
    _prefix, request_id, decision, scope = parts
    if decision not in ("allow", "deny"):
        return None
    remember_scope: RememberScope | None = None if scope == "-" else scope  # type: ignore[assignment]
    return request_id, (decision, remember_scope)  # type: ignore[return-value]


async def ask_via_telegram(
    sender: TelegramSender,
    pending: PendingApprovals,
    chat_id: int,
    tool_name: str,
    arguments: dict[str, Any],
    risk_description: str,
) -> PermissionModalResult:
    request_id, future = pending.register()
    buttons = [
        (label, _encode_callback_data(request_id, result)) for label, result in _BUTTONS
    ]
    await sender.send_message(
        chat_id, _format_prompt(tool_name, arguments, risk_description), buttons=buttons
    )
    return await future
