"""Cross-platform resource-limiting helpers.

POSIX gets real rlimits via preexec_fn. Windows has no equivalent stdlib API
(no `resource` module) — it relies solely on the wall-clock timeout plus
psutil-based process-tree cleanup, which works identically on both platforms
and is the actual safety net in RestrictedSubprocessSandbox either way.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

import psutil


def is_posix() -> bool:
    return sys.platform != "win32"


def make_posix_preexec_fn(
    *, cpu_seconds: int | None, memory_bytes: int | None
) -> Callable[[], None] | None:
    if not is_posix():
        return None

    def _preexec() -> None:
        import resource

        # No os.setsid() here: the caller already passes start_new_session=True
        # to Popen/create_subprocess_exec, which calls setsid() itself right
        # after forking, before running this preexec_fn. Calling it again on
        # a process that's already a session leader raises EPERM, which
        # crashes the whole preexec_fn (surfaces as "Exception occurred in
        # preexec_fn.") and every single shell command would fail.
        if cpu_seconds is not None:
            try:
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
            except (ValueError, OSError):
                pass
        if memory_bytes is not None:
            try:
                resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
            except (ValueError, OSError):
                pass  # not supported on every POSIX platform (notably macOS)

    return _preexec


def kill_process_tree(pid: int) -> None:
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    for child in parent.children(recursive=True):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    try:
        parent.kill()
    except psutil.NoSuchProcess:
        pass
