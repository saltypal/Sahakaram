"""Python CLI; MCP stdout is reserved exclusively for protocol messages."""

import getpass
import importlib.util
import logging
import webbrowser
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from pydantic import ValidationError

from sahakaram import __version__
from sahakaram.config import Settings
from sahakaram.logging import configure_logging

app = typer.Typer(help="Sahakaram: Python collaboration foundation.", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the installed Sahakaram version."""
    typer.echo(__version__)


@app.command()
def mcp() -> None:
    """Run the Phase 3 MCP server over stdin/stdout until the client disconnects."""
    try:
        settings = Settings()
    except ValidationError as exc:
        # Never echo invalid values: future settings may contain credentials.
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        typer.echo(f"Invalid Sahakaram configuration: {fields}", err=True)
        raise typer.Exit(code=2) from None

    configure_logging(settings.log_level)
    if importlib.util.find_spec("mcp") is None:
        typer.echo("Install the optional MCP interface with: uv sync --extra plugin", err=True)
        raise typer.Exit(2)
    from sahakaram.server import create_server

    if settings.auto_start_control:
        from sahakaram.bootstrap import ensure_control

        try:
            ensure_control(settings)
        except (RuntimeError, OSError) as exc:
            logging.getLogger(__name__).warning("Local control unavailable: %s", exc)

    logging.getLogger(__name__).info("Starting Phase 3 MCP server over stdio")
    create_server(settings).run(transport="stdio")


@app.command("plugin-mcp")
def plugin_mcp() -> None:
    """Run the bundled MCP with persistent per-user state outside the plugin cache."""
    import os

    os.environ.setdefault("SAHAKARAM_STATE_DIR", str(Path.home() / ".sahakaram"))
    mcp()


@app.command()
def connect(url: str) -> None:
    """Save the HTTPS room host used by this local MCP instance."""
    from sahakaram.bridge import BridgeError, save_connection

    try:
        save_connection(Settings(), url)
    except BridgeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    typer.echo("Room host saved. Reconnect the MCP client if it is already running.")


@app.command()
def share(port: Annotated[int, typer.Option(min=1, max=65535)] = 8788) -> None:
    """Publish only the room hub through an explicitly started Microsoft Dev Tunnel."""
    import shutil
    import subprocess

    executable = shutil.which("devtunnel")
    if not executable:
        typer.echo(
            "Install devtunnel and run devtunnel user login first; see docs/friend-demo.md",
            err=True,
        )
        raise typer.Exit(2)
    typer.echo("Keep this terminal open. Share the HTTPS URL and a separate one-use invitation.")
    try:
        result = subprocess.run(
            [executable, "host", "-p", str(port), "--allow-anonymous"],
            check=False,
        )
    except KeyboardInterrupt:
        return
    if result.returncode:
        raise typer.Exit(result.returncode)


@app.command()
def control(
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8787, browser: bool = False
) -> None:
    """Run the authenticated local account/profile control API on loopback."""
    import uvicorn
    from filelock import FileLock, Timeout

    from sahakaram.adapters.codex import CodexAdapter
    from sahakaram.local_api import create_local_app

    settings = Settings()
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(settings.log_level)
    factory = None
    if settings.codex_bin:
        selected = str(settings.codex_bin.resolve())

        def factory(home: Path) -> CodexAdapter:
            return CodexAdapter(home, codex_bin=selected)

    try:
        with FileLock(str(settings.state_dir / "control-service.lock"), timeout=0):
            application = create_local_app(settings.state_dir, factory)
            if browser:
                webbrowser.open(f"http://127.0.0.1:{port}/docs")
            uvicorn.run(
                application, host="127.0.0.1", port=port, log_level=settings.log_level.lower()
            )
    except Timeout:
        typer.echo("A local control service already owns this state directory", err=True)
        raise typer.Exit(2) from None


@app.command()
def dashboard() -> None:
    """Open local API controls; the themed dashboard is Phase 4."""
    from sahakaram.bootstrap import ensure_control

    settings = Settings()
    ensure_control(settings)
    webbrowser.open(f"http://127.0.0.1:{settings.control_port}/docs")


@app.command()
def setup(displayname: Annotated[str, typer.Option(prompt=True)]) -> None:
    """Create the local profile through the loopback service; passphrase stays off argv."""
    import httpx

    from sahakaram.bootstrap import ensure_control

    settings = Settings()
    ensure_control(settings)
    phrase = getpass.getpass("New local unlock passphrase (12+ characters): ")
    if phrase != getpass.getpass("Confirm passphrase: "):
        typer.echo("Passphrases differ", err=True)
        raise typer.Exit(2)
    with httpx.Client(trust_env=False) as client:
        response = client.post(
            f"http://127.0.0.1:{settings.control_port}/api/profile",
            json={"displayname": displayname, "passphrase": phrase},
            timeout=15,
        )
    if response.status_code != 201:
        typer.echo("Profile setup was rejected; check local control API", err=True)
        raise typer.Exit(2)
    typer.echo("Profile created. Run sahakaram unlock to authorize local MCP hosting actions.")


@app.command()
def unlock() -> None:
    """Unlock the local profile interactively and store its short-lived session locally."""
    import httpx

    from sahakaram.bootstrap import ensure_control
    from sahakaram.bridge import atomic_json

    settings = Settings()
    ensure_control(settings)
    phrase = getpass.getpass("Local unlock passphrase: ")
    with httpx.Client(trust_env=False) as client:
        response = client.post(
            f"http://127.0.0.1:{settings.control_port}/api/unlock",
            json={"passphrase": phrase},
            timeout=15,
        )
    if response.status_code != 200:
        typer.echo("Unlock rejected", err=True)
        raise typer.Exit(2)
    atomic_json(settings.state_dir / "control-session.json", response.json())
    typer.echo("Unlocked. Session token stored locally; it was not printed.")


@app.command()
def login(account_id: UUID, browser: bool = False) -> None:
    """Begin provider login for one isolated account after local profile unlock."""
    import asyncio

    from sahakaram.bridge import BridgeError, LocalBridge

    try:
        result = asyncio.run(
            LocalBridge(Settings()).request("POST", f"/api/accounts/{account_id.hex}/login")
        )
    except BridgeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    typer.echo("Complete provider login using this browser URL:")
    typer.echo(result["auth_url"])
    if browser:
        webbrowser.open(result["auth_url"])


@app.command()
def lock() -> None:
    """Invalidate local unlock sessions and close their owned Codex runtimes."""
    import asyncio

    from sahakaram.bridge import BridgeError, LocalBridge

    settings = Settings()
    try:
        asyncio.run(LocalBridge(settings).request("POST", "/api/lock"))
    except BridgeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    (settings.state_dir / "control-session.json").unlink(missing_ok=True)
    typer.echo("Local profile locked.")


def database_url(settings: Settings) -> str:
    from sahakaram.storage.database import default_database_url

    return (
        settings.database_url.get_secret_value()
        if settings.database_url
        else default_database_url(settings.state_dir)
    )


@app.command("db-upgrade")
def db_upgrade() -> None:
    """Apply forward-only Alembic migrations to the explicitly configured database."""
    from sahakaram.storage.database import create_database, migrate

    engine = create_database(database_url(Settings()))
    try:
        migrate(engine)
    finally:
        engine.dispose()
    typer.echo("Database upgraded to the current schema.")


@app.command()
def hub(
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8788,
    session_minutes: Annotated[int, typer.Option(min=1, max=1440)] = 15,
) -> None:
    """Run the standalone SQLite backend; initialize its local file automatically."""
    from sahakaram.backend import run_backend

    run_backend(Settings(), port=port, session_minutes=session_minutes)


@app.command()
def doctor() -> None:
    """Inspect capabilities without installing services, logging in, or changing schemas."""
    import json

    import httpx

    settings = Settings()
    report = {
        "python_package": __version__,
        "codex_sdk_installed": importlib.util.find_spec("openai_codex") is not None,
        "database_backend": "sqlite",
        "database_configured": True,
        "local_control_running": False,
    }
    try:
        with httpx.Client(timeout=1, trust_env=False) as client:
            response = client.get(f"http://127.0.0.1:{settings.control_port}/")
            report["local_control_running"] = (
                response.status_code == 200 and response.json().get("service") == "sahakaram-local"
            )
    except (httpx.HTTPError, ValueError):
        pass
    typer.echo(json.dumps(report, indent=2))
