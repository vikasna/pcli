"""Sortable id generation, without pulling in a ULID dependency.

Ids are `<13 hex digits of epoch-millis><10 hex digits of randomness>` so that
lexicographic sort order matches creation order at millisecond resolution.
"""

from __future__ import annotations

import secrets
import time


def new_id(prefix: str = "") -> str:
    millis = int(time.time() * 1000)
    return f"{prefix}{millis:013x}{secrets.token_hex(5)}"
