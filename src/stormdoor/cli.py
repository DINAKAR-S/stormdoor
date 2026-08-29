"""Command line: run the gateway, issue keys, read the ledger.

Key management runs against the database directly rather than over HTTP, so
you can mint the first key before the server is up, which is otherwise a
chicken-and-egg problem on a fresh install.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from .config import Settings, get_settings
from .pricing import PriceBook
from .store import Store

cli = typer.Typer(
    add_completion=False,
    help="stormdoor: an LLM gateway that proves itself under failure.",
)
keys_cli = typer.Typer(help="Create and inspect virtual API keys.")
cli.add_typer(keys_cli, name="keys")


def _store(settings: Settings) -> Store:
    return Store(settings.db_path)


@cli.command()
def serve(
    host: str = typer.Option(None, help="Bind address. Defaults to STORMDOOR_HOST."),
    port: int = typer.Option(None, help="Port. Defaults to STORMDOOR_PORT."),
    reload: bool = typer.Option(False, help="Reload on source changes, for development."),
    chaos: bool = typer.Option(False, "--chaos", help="Enable fault injection on this process."),
) -> None:
    """Run the gateway."""
    import uvicorn

    settings = get_settings()
    if chaos:
        settings.chaos_enabled = True

    uvicorn.run(
        "stormdoor.app:app",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
        log_level=settings.log_level,
    )


@keys_cli.command("create")
def keys_create(
    name: str = typer.Argument(..., help="A label for the key, shown in listings."),
    budget: float | None = typer.Option(None, help="Hard budget ceiling in USD."),
    rpm: int | None = typer.Option(None, help="Requests per minute."),
    tpm: int | None = typer.Option(None, help="Tokens per minute."),
    model: list[str] = typer.Option([], "--model", help="Restrict to these models. Repeatable."),
) -> None:
    """Issue a key. The secret is printed once and is not recoverable afterwards."""
    settings = get_settings()
    store = _store(settings)
    key, secret = asyncio.run(
        store.create_key(
            name=name, budget_usd=budget, rpm=rpm, tpm=tpm, allowed_models=list(model)
        )
    )
    typer.echo(json.dumps(key.public(), indent=2))
    typer.secho(f"\nsecret: {secret}", fg=typer.colors.GREEN, bold=True)
    typer.secho("Store it now. Only its hash is kept.", fg=typer.colors.YELLOW)


@keys_cli.command("list")
def keys_list() -> None:
    """List every key and what it has spent."""
    store = _store(get_settings())
    rows = asyncio.run(store.list_keys())
    if not rows:
        typer.echo("no keys yet. Create one with: stormdoor keys create <name>")
        raise typer.Exit()
    typer.echo(json.dumps([k.public() for k in rows], indent=2))


@keys_cli.command("usage")
def keys_usage(
    key_id: str = typer.Argument(..., help="The key id, not the secret."),
    limit: int = typer.Option(25, help="How many recent records to show."),
) -> None:
    """Show totals and recent requests for one key."""
    store = _store(get_settings())
    key = asyncio.run(store.key_by_id(key_id))
    if key is None:
        typer.secho(f"no key with id {key_id!r}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    summary = asyncio.run(store.usage_summary(key_id, limit=limit))
    typer.echo(json.dumps({"key": key.public(), **summary}, indent=2))


@keys_cli.command("disable")
def keys_disable(key_id: str) -> None:
    """Disable a key immediately."""
    store = _store(get_settings())
    if not asyncio.run(store.set_enabled(key_id, False)):
        typer.secho(f"no key with id {key_id!r}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.echo(f"{key_id} disabled")


@cli.command("admin-token")
def admin_token(
    reset: bool = typer.Option(False, "--reset", help="Replace it with a new one."),
) -> None:
    """Print the token that signs you in to the dashboard.

    This is the answer to "where do I find the token". It is stored beside the
    keys and does not change when the gateway restarts.
    """
    settings = get_settings()

    if settings.admin_token:
        typer.secho("Set by STORMDOOR_ADMIN_TOKEN in the environment:",
                    fg=typer.colors.YELLOW)
        typer.secho(settings.admin_token, fg=typer.colors.GREEN, bold=True)
        typer.echo("\nThe stored token is ignored while that variable is set.")
        raise typer.Exit()

    store = _store(settings)
    if reset:
        import secrets as _secrets

        new = _secrets.token_hex(16)
        store.set_setting("admin_token", new)
        typer.secho("New admin token, the previous one no longer works:",
                    fg=typer.colors.YELLOW)
        typer.secho(new, fg=typer.colors.GREEN, bold=True)
        raise typer.Exit()

    token, created = store.ensure_admin_token()
    typer.secho("Generated and stored:" if created else "Admin token:",
                fg=typer.colors.YELLOW)
    typer.secho(token, fg=typer.colors.GREEN, bold=True)
    typer.echo(f"\nStored in {settings.db_path}. Sign in at http://{settings.host}:{settings.port}")


@cli.command("pricing")
def pricing(
    file: Path | None = typer.Option(None, help="A pricing override file to inspect."),
) -> None:
    """Print the active rate card, with the source and date of every rate."""
    book = PriceBook.load(file or get_settings().pricing_file)
    rows = {
        model: {
            "input_per_mtok": price.input_per_mtok,
            "output_per_mtok": price.output_per_mtok,
            "source": price.source,
            "checked_on": price.checked_on,
        }
        for model, price in sorted(book._prices.items())  # noqa: SLF001
    }
    typer.echo(json.dumps(rows, indent=2))
    typer.secho(
        "\nRates go stale. Re-check them against the provider's pricing page "
        "and update checked_on.",
        fg=typer.colors.YELLOW,
    )


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
