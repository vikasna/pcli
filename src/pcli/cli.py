"""Typer entry point. `pcli` with no subcommand launches the TUI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from pcli.config.paths import data_dir
from pcli.config.settings import add_local_api_gateway, get_settings, update_config_file
from pcli.cost.tracker import global_cost_report
from pcli.session.directory_check import directory_mismatch
from pcli.session.export import export_session
from pcli.session.importer import import_session
from pcli.session.store import SessionNotFoundError, SessionStore
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
