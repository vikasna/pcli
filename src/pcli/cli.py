"""Typer entry point. `pcli` with no subcommand launches the TUI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from pcli.config.paths import data_dir
from pcli.config.settings import get_settings
from pcli.cost.tracker import global_cost_report
from pcli.session.export import export_session
from pcli.session.importer import import_session
from pcli.session.store import SessionStore
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
    settings = get_settings(**overrides)
    ctx.obj = settings

    if ctx.invoked_subcommand is None:
        from pcli.tui.app import PcliApp

        PcliApp(settings).run()


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
def toolbox_discover(name: str) -> None:
    from pcli.llm.client import GatewayClient
    from pcli.tools.toolbox.manager import ToolboxDiscoveryError, ToolboxManager

    async def _run() -> None:
        settings = get_settings()
        manager = ToolboxManager(cwd=Path.cwd())
        client = GatewayClient(settings) if settings.is_configured() else None
        try:
            summary = await manager.discover(
                name, gateway_client=client, model=settings.default_model or None
            )
            typer.echo(summary)
        except ToolboxDiscoveryError as exc:
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
