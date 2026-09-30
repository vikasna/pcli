"""Typer entry point. `pcli` with no subcommand launches the TUI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from pcli.config.paths import data_dir
from pcli.config.settings import add_local_api_gateway, get_settings, update_config_file
from pcli.cost.tracker import global_cost_report
from pcli.scheduler.runner import ScheduledSessionNotFoundError, run_task_once
from pcli.session.directory_check import directory_mismatch
from pcli.session.export import export_session
from pcli.session.importer import import_session
from pcli.session.store import SessionNotFoundError, SessionStore
from pcli.telegram.bot import run_telegram_daemon
from pcli.util.logging import configure_logging

app = typer.Typer(add_completion=False, no_args_is_help=False)

sessions_app = typer.Typer(help="Manage stored sessions.")
app.add_typer(sessions_app, name="sessions")

cost_app = typer.Typer(help="Cost reporting.")
app.add_typer(cost_app, name="cost")

toolbox_app = typer.Typer(help="Discover OS/software tools for the agent to use.")
app.add_typer(toolbox_app, name="toolbox")

schedule_app = typer.Typer(help="Crontab-like recurring task scheduling (requires the "
    "'schedule' extra: pip install -e '.[schedule]').")
app.add_typer(schedule_app, name="schedule")

tools_app = typer.Typer(help="Inspect the built-in tool set.")
app.add_typer(tools_app, name="tools")


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    gateway_url: str = typer.Option(None, "--gateway-url", help="Override the gateway base URL."),
    api_key: str = typer.Option(None, "--api-key", help="Override the gateway API key."),
    model: str = typer.Option(None, "--model", help="Override the default model."),
    artifact_threshold: int = typer.Option(
        None,
        "--artifact-threshold",
        help="Override the artifact-archiving threshold, in characters "
        "(tool results longer than this get truncated + archived; see fetch_artifact).",
    ),
    local_api: bool = typer.Option(
        False,
        "--local-api",
        help="Mark the active gateway as local-api mode: uncaps max_tool_iterations and the "
        "guardrails' max_tool_calls_per_turn/per_minute, and forces cost to $0 instead of "
        "looking it up in the pricing table. Paired to (and persisted with) whichever "
        "gateway is active for this invocation.",
    ),
    resume: str = typer.Option(
        None,
        "--resume",
        "-r",
        help="Resume a past session by id (see 'pcli sessions list'; the printed hint on quit "
        "gives the exact command).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
) -> None:
    configure_logging(verbose=verbose)
    overrides = {}
    if gateway_url:
        overrides["gateway_base_url"] = gateway_url
    if api_key:
        overrides["gateway_api_key"] = api_key
    if model:
        overrides["default_model"] = model
    if artifact_threshold is not None:
        overrides["artifact_threshold_chars"] = artifact_threshold
    if gateway_url or model or artifact_threshold is not None:
        # Remembered for next time so a bare `pcli` picks it up — deliberately
        # not persisting api_key here, so a secret passed via --api-key isn't
        # silently written to disk.
        update_config_file(
            gateway_base_url=gateway_url,
            default_model=model,
            artifact_threshold_chars=artifact_threshold,
        )
    settings = get_settings(**overrides)

    if local_api:
        if not settings.gateway_base_url:
            typer.echo(
                "--local-api needs a gateway to pair with; pass --gateway-url too "
                "(or configure one first).",
                err=True,
            )
        else:
            add_local_api_gateway(settings.gateway_base_url)
            if settings.gateway_base_url not in settings.local_api_gateways:
                settings.local_api_gateways.append(settings.gateway_base_url)

    ctx.obj = settings

    if ctx.invoked_subcommand is None:
        from pcli.tui.app import PcliApp

        store = SessionStore()
        resumed_session = None
        if resume:
            try:
                resumed_session = store.load(resume)
            except SessionNotFoundError:
                typer.echo(
                    f"No session found with id '{resume}'. Run 'pcli sessions list' to see "
                    "available sessions.",
                    err=True,
                )
                raise typer.Exit(code=1) from None
            warning = directory_mismatch(resumed_session, Path.cwd())
            if warning and not typer.confirm(f"{warning}\nContinue anyway?", default=False):
                raise typer.Exit(code=0)

        PcliApp(settings, session=resumed_session).run()
        _print_resume_hint(store)


def _print_resume_hint(store: SessionStore) -> None:
    """The session most recently touched by the run that just ended - not
    necessarily `resumed_session` above, since /sessions can switch to a
    different one mid-run. Skips a session with zero messages: it gets
    silently pruned on the next launch (SessionStore.prune_empty_sessions),
    so a resume hint for it would go stale immediately."""
    entries = store.list_index()
    if not entries or entries[0].message_count == 0:
        return
    latest = entries[0]
    typer.echo(f"\nResume this session anytime with: pcli --resume {latest.id}")


@app.command("run")
def run_command(
    task: str | None = typer.Option(None, "--task", help="The task to run, given inline."),
    task_file: str | None = typer.Option(
        None,
        "--task-file",
        help="Path to a file containing the task (for longer, step-by-step instructions you "
        "want to reuse - e.g. on a schedule via cron/Task Scheduler). Exactly one of --task/"
        "--task-file is required.",
    ),
    session: str | None = typer.Option(
        None,
        "--session",
        help="Resume/append to an existing session by id (see 'pcli sessions list'), instead "
        "of starting a fresh one.",
    ),
    quiet: bool = typer.Option(
        False, "--quiet", help="Only print the final answer, not tool-call progress."
    ),
    headed: bool = typer.Option(
        False,
        "--headed",
        help="Show the browser window if browser_* tools are used, instead of running it "
        "headless (the default for a scheduled/unattended run - nothing to show, and a real "
        "window shouldn't pop up unattended).",
    ),
    notify_telegram_flag: bool = typer.Option(
        False,
        "--notify-telegram",
        help="Also send the final answer to the configured Telegram chat once this run "
        "finishes (see 'pcli telegram --help'). Requires telegram_bot_token/telegram_chat_id "
        "to already be configured - skipped with a warning otherwise, never a hard failure.",
    ),
    max_cost: float | None = typer.Option(
        None,
        "--max-cost",
        help="Hard cap on this run's total spend (USD), overriding max_session_cost_usd for "
        "just this invocation - never persisted to config.toml, never affects the TUI or any "
        "other run. Omit to use whatever max_session_cost_usd is already configured (unset by "
        "default - no cap).",
    ),
) -> None:
    """Runs a single task non-interactively and exits - no TUI. Meant to be
    invoked by an OS scheduler (cron / Task Scheduler) or pcli's own
    scheduler (`pcli schedule`) for a task you've already worked out
    interactively once. Anything not already granted "Always Allow" (see
    the TUI's permission prompt) is refused rather than prompted for, since
    there's no one here to ask - set those up interactively first if this
    task needs them."""
    if bool(task) == bool(task_file):
        typer.echo("Provide exactly one of --task or --task-file.", err=True)
        raise typer.Exit(code=1)
    if task_file:
        task = Path(task_file).read_text(encoding="utf-8")
    assert task is not None

    async def _run() -> None:
        settings = get_settings()
        if not settings.is_configured():
            typer.echo(
                "Gateway not configured. Set PCLI_GATEWAY_URL (and PCLI_GATEWAY_API_KEY if "
                "your gateway requires auth) or edit the config file first.",
                err=True,
            )
            raise typer.Exit(code=1)
        if max_cost is not None:
            # A local override, not get_settings(max_session_cost_usd=...) -
            # that helper rebuilds the whole cached Settings singleton from
            # scratch using only the given overrides (config/settings.py's
            # get_settings), which would silently drop --gateway-url/--api-key/
            # --model overrides _root's own callback already applied earlier
            # in this same invocation. model_copy starts from the already-
            # fully-resolved settings instead, so only this one field changes.
            settings = settings.model_copy(update={"max_session_cost_usd": max_cost})

        store = SessionStore()
        cwd = Path.cwd()

        def on_progress(line: str) -> None:
            if not quiet:
                typer.echo(line)

        try:
            result = await run_task_once(
                task,
                settings=settings,
                store=store,
                cwd=cwd,
                session_id=session,
                quiet=quiet,
                headed=headed,
                notify_telegram_flag=notify_telegram_flag,
                on_progress=on_progress,
            )
        except ScheduledSessionNotFoundError:
            typer.echo(f"No session found with id '{session}'.", err=True)
            raise typer.Exit(code=1) from None
        except Exception as exc:
            typer.echo(f"Run failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        typer.echo(f"\n{result.final_text}" if not quiet else result.final_text)
        typer.echo(f"\nSession: {result.session.id} (resume with: pcli --resume {result.session.id})")

        if result.terminated_early or result.truncations_exhausted:
            raise typer.Exit(code=1)

    asyncio.run(_run())


@app.command("telegram")
def telegram_command() -> None:
    """Runs pcli as a long-running Telegram bot - the third way to run
    pcli, alongside the interactive TUI and one-shot `pcli run`. Requires
    telegram_bot_token (from @BotFather) and telegram_chat_id (the one
    chat this bot will talk to - everything else is silently ignored) to
    already be configured; refuses to start otherwise rather than running
    unsecured. Keeps one ongoing session for that chat (reset with the
    bot's own /new command), driven by the same AgentLoop machinery as
    everything else - a consequential tool call is approved or denied via
    an inline-keyboard prompt in the chat, exactly like the TUI's own
    permission modal. Runs until interrupted (Ctrl+C)."""

    async def _run() -> None:
        settings = get_settings()
        if not settings.is_configured():
            typer.echo(
                "Gateway not configured. Set PCLI_GATEWAY_URL (and PCLI_GATEWAY_API_KEY if "
                "your gateway requires auth) or edit the config file first.",
                err=True,
            )
            raise typer.Exit(code=1)
        if not settings.is_telegram_configured():
            typer.echo(
                "telegram_bot_token and telegram_chat_id must both be set (env vars "
                "PCLI_TELEGRAM_BOT_TOKEN / PCLI_TELEGRAM_CHAT_ID, or the constructor kwargs) "
                "before pcli telegram can start.",
                err=True,
            )
            raise typer.Exit(code=1)

        cwd = Path.cwd()

        def on_ready(session_id: str) -> None:
            typer.echo(f"Listening for chat {settings.telegram_chat_id}. Session: {session_id}")
            typer.echo("Press Ctrl+C to stop.")

        try:
            await run_telegram_daemon(settings, cwd, on_ready=on_ready)
        except Exception as exc:
            typer.echo(f"Startup failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        typer.echo("\nStopped.")


@sessions_app.command("list")
def sessions_list() -> None:
    store = SessionStore()
    for entry in store.list_index():
        typer.echo(
            f"{entry.id}  {entry.updated_at:%Y-%m-%d %H:%M}  "
            f"${entry.total_cost_usd:.4f}  {entry.title}"
        )


@sessions_app.command("export")
def sessions_export(
    session_id: str,
    out: str = typer.Option(None, "--out", help="Output path."),
    use_gzip: bool = typer.Option(False, "--gzip", help="Gzip-compress the export."),
) -> None:
    store = SessionStore()
    session = store.load(session_id)
    suffix = ".pcli-session.json.gz" if use_gzip else ".pcli-session.json"
    out_path = Path(out) if out else data_dir() / "exports" / f"{session.id}{suffix}"
    export_session(session, out_path, store=store, use_gzip=use_gzip or None)
    typer.echo(f"Exported to {out_path}")


@sessions_app.command("import")
def sessions_import(
    path: str,
    restore_grants: bool = typer.Option(
        False, "--restore-grants", help="Also restore 'always allow' permission grants."
    ),
) -> None:
    store = SessionStore()
    session = import_session(Path(path), store=store, restore_grants=restore_grants)
    typer.echo(f"Imported as session {session.id}")


@cost_app.command("report")
def cost_report_command() -> None:
    typer.echo(json.dumps(global_cost_report(), indent=2))


@toolbox_app.command("discover")
def toolbox_discover(
    name: str,
    path: str = typer.Option(
        None, "--path", help="Register a self-authored script directly, bypassing PATH lookup."
    ),
) -> None:
    from pcli.llm.client import GatewayClient
    from pcli.llm.errors import GatewayError
    from pcli.sandbox.base import SandboxSecurityError
    from pcli.tools.toolbox.manager import ToolboxDiscoveryError, ToolboxManager

    async def _run() -> None:
        settings = get_settings()
        manager = ToolboxManager(cwd=Path.cwd())
        client = GatewayClient(settings) if settings.is_configured() else None
        try:
            summary = await manager.discover(
                name, gateway_client=client, model=settings.default_model or None, path=path
            )
            typer.echo(summary)
        except (ToolboxDiscoveryError, GatewayError, SandboxSecurityError) as exc:
            # discover() calls the gateway to synthesize tool schemas when
            # there's no curated plugin - that can fail same as any other
            # gateway call (previously uncaught here, crashing with a raw
            # traceback instead of a clean message).
            typer.echo(f"Discovery failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            if client is not None:
                await client.aclose()

    asyncio.run(_run())


@toolbox_app.command("list")
def toolbox_list() -> None:
    from pcli.tools.toolbox import store

    registry = store.read_registry()
    if not registry:
        typer.echo("No software discovered yet. Run: pcli toolbox discover <name>")
        return
    for name, entry in registry.items():
        typer.echo(
            f"{name}  [{entry['source']}]  {entry.get('version', '?')}  "
            f"{entry.get('tool_count', 0)} tool(s)"
        )


@toolbox_app.command("remove")
def toolbox_remove(name: str) -> None:
    from pcli.tools.toolbox import store

    registry = store.read_registry()
    if name not in registry:
        typer.echo(f"'{name}' is not in the toolbox.")
        raise typer.Exit(code=1)
    registry.pop(name)
    store.write_registry(registry)
    typer.echo(f"Removed '{name}' from the toolbox.")


@schedule_app.command("add")
def schedule_add(
    cron: str | None = typer.Option(None, "--cron", help="Standard 5-field cron expression, "
        "e.g. '*/15 * * * *' (minute hour day month weekday). Exactly one of --cron/"
        "--on-file-change/--on-git-commit is required."),
    on_file_change: str | None = typer.Option(
        None, "--on-file-change", help="Fire whenever this file or directory (watched "
        "recursively) changes, instead of on a cron schedule."
    ),
    on_git_commit: bool = typer.Option(
        False, "--on-git-commit", help="Fire whenever a new commit lands on the watched "
        "branch, instead of on a cron schedule. See --git-repo/--git-branch."
    ),
    git_repo: str | None = typer.Option(
        None, "--git-repo", help="--on-git-commit only: repo to watch. Defaults to the "
        "scheduler daemon's own working directory."
    ),
    git_branch: str | None = typer.Option(
        None, "--git-branch", help="--on-git-commit only: branch to watch. Defaults to "
        "whatever's currently checked out."
    ),
    task: str | None = typer.Option(None, "--task", help="The task to run, given inline."),
    task_file: str | None = typer.Option(
        None, "--task-file", help="Path to a file containing the task. Exactly one of "
        "--task/--task-file is required."
    ),
    name: str = typer.Option("", "--name", help="A friendly label shown in 'pcli schedule list'."),
    session: str | None = typer.Option(
        None, "--session", help="Append to this existing session on every run, instead of "
        "starting a fresh one each time."
    ),
    headed: bool = typer.Option(False, "--headed", help="Show the browser window, if used."),
    notify_telegram_flag: bool = typer.Option(
        False, "--notify-telegram", help="Send the final answer to the configured Telegram "
        "chat after each run."
    ),
    quiet: bool = typer.Option(
        True, "--quiet/--no-quiet", help="Only keep the final answer in the run's progress "
        "log, not tool-call-by-tool-call output."
    ),
    max_cost: float | None = typer.Option(
        None, "--max-cost", help="Hard cap on this job's own runs (USD), overriding "
        "max_session_cost_usd just for it - never persisted to config.toml, never affects "
        "other jobs or the TUI. Omit to use whatever max_session_cost_usd is already "
        "configured (unset by default - no cap)."
    ),
) -> None:
    """Adds a new recurring job. Nothing runs until 'pcli schedule run' (the
    daemon) is actually started - adding a job only saves it. Exactly one
    of --cron/--on-file-change/--on-git-commit selects the trigger; the job
    fires either on that time schedule or the next time the watched
    file/commit changes (scheduler/triggers.py, polled by the daemon the
    same way a cron job's due time is)."""
    from croniter import croniter

    from pcli.scheduler.models import ScheduleJob
    from pcli.scheduler.store import add_job

    if bool(task) == bool(task_file):
        typer.echo("Provide exactly one of --task or --task-file.", err=True)
        raise typer.Exit(code=1)

    triggers_given = sum(1 for t in (cron, on_file_change, on_git_commit) if t)
    if triggers_given != 1:
        typer.echo(
            "Provide exactly one of --cron, --on-file-change, or --on-git-commit.", err=True
        )
        raise typer.Exit(code=1)

    if cron is not None:
        if not croniter.is_valid(cron):
            typer.echo(f"'{cron}' isn't a valid 5-field cron expression.", err=True)
            raise typer.Exit(code=1)
        job = ScheduleJob(
            name=name,
            trigger="cron",
            cron=cron,
            task=task,
            task_file=task_file,
            session_id=session,
            headed=headed,
            notify_telegram=notify_telegram_flag,
            quiet=quiet,
            max_cost_usd=max_cost,
        )
        description = cron
    elif on_file_change is not None:
        job = ScheduleJob(
            name=name,
            trigger="file_change",
            watch_path=on_file_change,
            task=task,
            task_file=task_file,
            session_id=session,
            headed=headed,
            notify_telegram=notify_telegram_flag,
            quiet=quiet,
            max_cost_usd=max_cost,
        )
        description = f"on change: {on_file_change}"
    else:
        job = ScheduleJob(
            name=name,
            trigger="git_commit",
            watch_git_repo=git_repo,
            watch_git_branch=git_branch,
            task=task,
            task_file=task_file,
            session_id=session,
            headed=headed,
            notify_telegram=notify_telegram_flag,
            quiet=quiet,
            max_cost_usd=max_cost,
        )
        description = f"on commit: {git_repo or '.'}" + (f" [{git_branch}]" if git_branch else "")

    add_job(job)
    typer.echo(f"Added job {job.id} ({description}). Start 'pcli schedule run' to begin executing it.")


@schedule_app.command("list")
def schedule_list() -> None:
    from pcli.scheduler.store import read_schedule

    jobs = read_schedule().jobs
    if not jobs:
        typer.echo("No scheduled jobs. Add one with: pcli schedule add --cron '...' --task '...'")
        return
    for job in jobs:
        state = "enabled" if job.enabled else "disabled"
        last = f"{job.last_status} @ {job.last_run_at.isoformat()}" if job.last_run_at else "never run"
        label = job.name or "(unnamed)"
        if job.trigger == "cron":
            next_run = job.next_run_at.isoformat() if job.next_run_at else "not yet computed"
            trigger_desc = f"[{job.cron}]  {state}  next: {next_run}"
        elif job.trigger == "file_change":
            trigger_desc = f"watching: {job.watch_path}  {state}"
        else:
            repo = job.watch_git_repo or "."
            branch = f" [{job.watch_git_branch}]" if job.watch_git_branch else ""
            trigger_desc = f"watching: commits on {repo}{branch}  {state}"
        typer.echo(f"{job.id}  {label}  {trigger_desc}  last: {last}")


@schedule_app.command("remove")
def schedule_remove(job_id: str) -> None:
    from pcli.scheduler.store import remove_job

    if not remove_job(job_id):
        typer.echo(f"No job found with id '{job_id}'.", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Removed job {job_id}.")


@schedule_app.command("enable")
def schedule_enable(job_id: str) -> None:
    from pcli.scheduler.store import set_job_enabled

    if not set_job_enabled(job_id, True):
        typer.echo(f"No job found with id '{job_id}'.", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Enabled job {job_id}.")


@schedule_app.command("disable")
def schedule_disable(job_id: str) -> None:
    from pcli.scheduler.store import set_job_enabled

    if not set_job_enabled(job_id, False):
        typer.echo(f"No job found with id '{job_id}'.", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Disabled job {job_id}.")


@schedule_app.command("run")
def schedule_run(
    poll_interval: int = typer.Option(
        30, "--poll-interval", help="Seconds between checking schedule.json for due jobs."
    ),
) -> None:
    """The daemon: runs until interrupted (Ctrl+C), firing each enabled
    job's task when its cron schedule says it's due. Point an OS-level
    scheduler (Task Scheduler/systemd/a 'nohup'/tmux session) at this
    command to keep it running - it is itself the thing that decides
    *when*, not something an external cron needs to re-invoke per job."""
    from pcli.agent.headless import HeadlessTurnResult
    from pcli.scheduler.daemon import run_scheduler_daemon
    from pcli.scheduler.models import ScheduleJob

    async def _run() -> None:
        settings = get_settings()
        if not settings.is_configured():
            typer.echo(
                "Gateway not configured. Set PCLI_GATEWAY_URL (and PCLI_GATEWAY_API_KEY if "
                "your gateway requires auth) or edit the config file first.",
                err=True,
            )
            raise typer.Exit(code=1)

        def on_job_run(
            job: ScheduleJob, result: HeadlessTurnResult | None, error: Exception | None
        ) -> None:
            if error is not None:
                typer.echo(f"[{job.id}] {job.name or job.cron} failed: {error}", err=True)
            else:
                typer.echo(f"[{job.id}] {job.name or job.cron} finished.")

        typer.echo(f"Scheduler running (polling every {poll_interval}s). Press Ctrl+C to stop.")
        await run_scheduler_daemon(settings, poll_interval_s=poll_interval, on_job_run=on_job_run)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        typer.echo("\nStopped.")


@schedule_app.command("run-now")
def schedule_run_now(job_id: str) -> None:
    """Fires one job immediately, bypassing its cron schedule - for testing
    a job works before trusting the daemon's own timing."""
    from pcli.scheduler.store import get_job

    job = get_job(job_id)
    if job is None:
        typer.echo(f"No job found with id '{job_id}'.", err=True)
        raise typer.Exit(code=1)

    task = job.task
    if job.task_file:
        task = Path(job.task_file).read_text(encoding="utf-8")
    assert task is not None

    async def _run() -> None:
        settings = get_settings()
        if not settings.is_configured():
            typer.echo(
                "Gateway not configured. Set PCLI_GATEWAY_URL (and PCLI_GATEWAY_API_KEY if "
                "your gateway requires auth) or edit the config file first.",
                err=True,
            )
            raise typer.Exit(code=1)

        store = SessionStore()
        try:
            result = await run_task_once(
                task,
                settings=settings,
                store=store,
                cwd=Path.cwd(),
                session_id=job.session_id,
                quiet=job.quiet,
                headed=job.headed,
                notify_telegram_flag=job.notify_telegram,
                on_progress=typer.echo,
            )
        except ScheduledSessionNotFoundError:
            typer.echo(f"No session found with id '{job.session_id}'.", err=True)
            raise typer.Exit(code=1) from None
        except Exception as exc:
            typer.echo(f"Run failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        typer.echo(f"\n{result.final_text}")

    asyncio.run(_run())


def _one_line_description(description: str, max_chars: int = 100) -> str:
    """Reduces a tool's (possibly multi-paragraph, possibly one very long
    paragraph) description to a single line for 'pcli tools list': keeps
    only the first line (drops anything after an internal "\\n\\n", e.g.
    web_search's "Query tips:" paragraph), then hard-truncates at a word
    boundary if that first line is itself still too long (e.g. web_fetch's
    description is one long paragraph with no internal newline at all)."""
    first_line = description.splitlines()[0].strip()
    if len(first_line) <= max_chars:
        return first_line
    truncated = first_line[:max_chars].rsplit(" ", 1)[0]
    return truncated + "..."


@tools_app.command("list")
def tools_list() -> None:
    """Lists every built-in tool (not toolbox-discovered or
    register_agent_tool-created ones, which are dynamic/session state -
    see 'pcli toolbox list' for the toolbox case) with its read-only/
    mutating type, permission/plan-mode requirements, and a one-line
    description."""
    from pcli.tools.registry import build_default_registry

    registry = build_default_registry()
    tools = sorted(registry, key=lambda t: t.name)
    name_width = max(len(t.name) for t in tools)
    for tool in tools:
        read_only_label = "read-only" if tool.read_only else "mutating"
        typer.echo(
            f"{tool.name:<{name_width}}  [{read_only_label:<9}]  "
            f"needs_permission={tool.needs_permission!s:<5}  "
            f"plan_mode_safe={tool.plan_mode_safe!s:<5}  "
            f"{_one_line_description(tool.description)}"
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
