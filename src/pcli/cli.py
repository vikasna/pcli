"""Typer entry point. `pcli` with no subcommand launches the TUI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from pcli.agent.headless import new_headless_session, run_headless_task
from pcli.agent.runtime import build_agent_runtime, build_permission_manager
from pcli.config.paths import data_dir
from pcli.config.settings import add_local_api_gateway, get_settings, update_config_file
from pcli.cost.tracker import global_cost_report
from pcli.session.directory_check import directory_mismatch
from pcli.session.export import export_session
from pcli.session.importer import import_session
from pcli.session.store import SessionNotFoundError, SessionStore
from pcli.telegram.bot import notify_telegram, run_telegram_daemon
from pcli.util.logging import configure_logging

app = typer.Typer(add_completion=False, no_args_is_help=False)

sessions_app = typer.Typer(help="Manage stored sessions.")
app.add_typer(sessions_app, name="sessions")

cost_app = typer.Typer(help="Cost reporting.")
app.add_typer(cost_app, name="cost")

toolbox_app = typer.Typer(help="Discover OS/software tools for the agent to use.")
app.add_typer(toolbox_app, name="toolbox")


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
) -> None:
    """Runs a single task non-interactively and exits - no TUI. Meant to be
    invoked by an OS scheduler (cron / Task Scheduler) for a task you've
    already worked out interactively once; pcli itself doesn't schedule
    anything. Anything not already granted "Always Allow" (see the TUI's
    permission prompt) is refused rather than prompted for, since there's
    no one here to ask - set those up interactively first if this task
    needs them."""
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

        store = SessionStore()
        cwd = Path.cwd()
        if session:
            try:
                headless_session = store.load(session)
            except SessionNotFoundError:
                typer.echo(f"No session found with id '{session}'.", err=True)
                raise typer.Exit(code=1) from None
        else:
            headless_session = new_headless_session(store, settings, cwd)

        try:
            runtime = await build_agent_runtime(settings, cwd, browser_headless=not headed)
        except Exception as exc:
            typer.echo(f"Startup failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        def on_progress(line: str) -> None:
            if not quiet:
                typer.echo(line)

        try:
            result = await run_headless_task(
                task,
                session=headless_session,
                runtime=runtime,
                settings=settings,
                permission_manager=build_permission_manager(settings),
                cwd=cwd,
                store=store,
                on_progress=on_progress,
            )
        finally:
            await runtime.client.aclose()
            await runtime.browser_session.close()

        typer.echo(f"\n{result.final_text}" if not quiet else result.final_text)
        typer.echo(f"\nSession: {result.session.id} (resume with: pcli --resume {result.session.id})")

        if notify_telegram_flag:
            if not settings.is_telegram_configured():
                typer.echo(
                    "--notify-telegram was given but telegram_bot_token/telegram_chat_id "
                    "aren't configured - skipping the notification.",
                    err=True,
                )
            else:
                try:
                    await notify_telegram(settings, result.final_text)
                except Exception as exc:  # noqa: BLE001 - a failed notification shouldn't fail the run
                    typer.echo(f"Failed to send the Telegram notification: {exc}", err=True)

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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
