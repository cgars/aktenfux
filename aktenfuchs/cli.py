"""Typer-based CLI for Aktenfuchs (afu)."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from aktenfuchs import __version__

app = typer.Typer(
    name="afu",
    help="Aktenfuchs – local, privacy-first document assistant for OCR-ready PDFs.",
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Config loading helper
# ---------------------------------------------------------------------------


def _load_config(config_path: Optional[Path], dry_run: Optional[bool]):
    """Load config and optionally override dry_run."""
    from aktenfuchs.config import load_config  # noqa: PLC0415

    try:
        cfg = load_config(config_path)
    except FileNotFoundError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    if dry_run is not None:
        cfg.dry_run = dry_run

    if cfg.dry_run:
        console.print("[yellow]⚠  DRY-RUN mode active – no files will be moved.[/yellow]")

    return cfg


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def version() -> None:
    """Show the Aktenfuchs version."""
    console.print(f"Aktenfuchs {__version__}")


@app.command()
def init(
    target: Optional[Path] = typer.Option(
        None,
        "--target",
        "-t",
        help="Directory in which to create config.yaml (defaults to current directory).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Initialise config.yaml and create the working folders."""
    _setup_logging(verbose)
    from aktenfuchs.config import init_config, load_config  # noqa: PLC0415

    target_dir = target or Path.cwd()
    config_file = init_config(target_dir)

    if config_file.stat().st_size > 0 and config_file.exists():
        console.print(f"[green]✓[/green] Config file ready: {config_file}")
    else:
        console.print(f"[green]✓[/green] Config created: {config_file}")

    # Create working directories.
    try:
        cfg = load_config(config_file)
    except Exception as exc:  # noqa: BLE001
        err_console.print(f"[red]Could not load config:[/red] {exc}")
        raise typer.Exit(1) from exc

    for folder in cfg.all_working_dirs():
        folder.mkdir(parents=True, exist_ok=True)
        console.print(f"  [dim]folder:[/dim] {folder}")

    console.print("[green]Initialisation complete.[/green]")
    if cfg.dry_run:
        console.print(
            "[yellow]Reminder:[/yellow] dry_run is ON in config.yaml. "
            "Set it to false when you are ready for production use."
        )


@app.command()
def setup(
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Check configuration, folders, Ollama availability, and run a small model test."""
    _setup_logging(verbose)
    from aktenfuchs.config import load_config  # noqa: PLC0415
    import aktenfuchs.ollama_manager as om  # noqa: PLC0415

    try:
        cfg = load_config(config_path)
    except FileNotFoundError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    console.rule("Aktenfuchs Setup Check")

    # Folders
    ok = True
    for folder in cfg.all_working_dirs():
        exists = folder.exists()
        status = "[green]✓[/green]" if exists else "[red]✗[/red]"
        console.print(f"  {status} {folder}")
        if not exists:
            ok = False

    # Ollama
    running = om.is_ollama_running(cfg.ollama_url)
    if running:
        console.print(f"[green]✓[/green] Ollama is reachable at {cfg.ollama_url}")
    else:
        console.print(
            f"[red]✗[/red] Ollama is not reachable at {cfg.ollama_url}. "
            "Please start Ollama first."
        )
        ok = False

    if running:
        models = om.list_models(cfg.ollama_url)
        model_installed = any(
            m == cfg.ollama_model or m.startswith(cfg.ollama_model + ":")
            for m in models
        )
        if model_installed:
            console.print(f"[green]✓[/green] Model '{cfg.ollama_model}' is installed.")
        else:
            console.print(
                f"[yellow]![/yellow] Model '{cfg.ollama_model}' is not installed. "
                "Run 'afu scan' once and you will be prompted to download it."
            )

        console.print("Running quick model test …")
        if om.test_model(cfg.ollama_model, cfg.ollama_url):
            console.print("[green]✓[/green] Model test passed.")
        else:
            console.print("[red]✗[/red] Model test failed.")
            ok = False

    if ok:
        console.print("[green]Setup check passed.[/green]")
    else:
        console.print("[red]Setup check found issues. Please fix them before scanning.[/red]")
        raise typer.Exit(1)


@app.command()
def scan(
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    dry_run: Optional[bool] = typer.Option(None, "--dry-run/--no-dry-run"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Process all PDFs in the _Inbox folder."""
    _setup_logging(verbose)
    cfg = _load_config(config_path, dry_run)

    from aktenfuchs.main import process_inbox  # noqa: PLC0415
    import aktenfuchs.ollama_manager as om  # noqa: PLC0415

    if not om.is_ollama_running(cfg.ollama_url):
        err_console.print(
            f"[red]Ollama is not reachable at {cfg.ollama_url}.[/red] "
            "Please start Ollama and try again."
        )
        raise typer.Exit(1)

    if not om.ensure_model(cfg.ollama_model, cfg.ollama_url):
        err_console.print(
            f"[red]Model '{cfg.ollama_model}' is not available.[/red]"
        )
        raise typer.Exit(1)

    process_inbox(cfg)


@app.command()
def review(
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """List documents in _Review waiting for approval."""
    _setup_logging(verbose)
    cfg = _load_config(config_path, dry_run=None)

    from aktenfuchs.review import list_review_documents, print_review_table  # noqa: PLC0415

    sidecars = list_review_documents(cfg.review_path)
    print_review_table(sidecars)


@app.command()
def approve(
    doc_id: str = typer.Argument(..., help="Document ID to approve."),
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    dry_run: Optional[bool] = typer.Option(None, "--dry-run/--no-dry-run"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Move a reviewed document from _Review into the Archive."""
    _setup_logging(verbose)
    cfg = _load_config(config_path, dry_run)

    from aktenfuchs.main import approve_document  # noqa: PLC0415

    try:
        approve_document(doc_id, cfg)
        if not cfg.dry_run:
            console.print(f"[green]✓[/green] Approved: {doc_id}")
    except FileNotFoundError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc


@app.command()
def reject(
    doc_id: str = typer.Argument(..., help="Document ID to reject."),
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    dry_run: Optional[bool] = typer.Option(None, "--dry-run/--no-dry-run"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Move a document from _Review to _Error."""
    _setup_logging(verbose)
    cfg = _load_config(config_path, dry_run)

    from aktenfuchs.main import reject_document  # noqa: PLC0415

    try:
        reject_document(doc_id, cfg)
        if not cfg.dry_run:
            console.print(f"[yellow]✓[/yellow] Rejected: {doc_id}")
    except FileNotFoundError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc


@app.command()
def status(
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show document counts for each folder."""
    _setup_logging(verbose)
    cfg = _load_config(config_path, dry_run=None)

    def _count_pdfs(path: Path) -> int:
        if not path.exists():
            return 0
        return sum(1 for _ in path.glob("*.pdf"))

    inbox_n = _count_pdfs(cfg.inbox_path)
    review_n = _count_pdfs(cfg.review_path)
    imported_n = _count_pdfs(cfg.imported_path)
    error_n = _count_pdfs(cfg.error_path)
    archive_n = sum(1 for _ in cfg.archive_path.rglob("*.pdf")) if cfg.archive_path.exists() else 0

    from rich.table import Table  # noqa: PLC0415

    table = Table(title="Aktenfuchs Status")
    table.add_column("Folder", style="cyan")
    table.add_column("Count", justify="right")

    table.add_row("_Inbox", str(inbox_n))
    table.add_row("_Review", str(review_n))
    table.add_row("_Imported", str(imported_n))
    table.add_row("_Error", str(error_n))
    table.add_row("Archive", str(archive_n))

    if cfg.use_sqlite_index and cfg.sqlite_path.exists():
        import aktenfuchs.db as db  # noqa: PLC0415
        counts = db.count_by_status(cfg.sqlite_path)
        table.add_section()
        for st, cnt in sorted(counts.items()):
            table.add_row(f"  DB: {st}", str(cnt))

    console.print(table)


@app.command()
def reprocess(
    doc_id: str = typer.Argument(..., help="Document ID to re-analyze."),
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    dry_run: Optional[bool] = typer.Option(None, "--dry-run/--no-dry-run"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Re-analyze a document from _Review with the LLM."""
    _setup_logging(verbose)
    cfg = _load_config(config_path, dry_run)

    from aktenfuchs.main import reprocess_document  # noqa: PLC0415
    import aktenfuchs.ollama_manager as om  # noqa: PLC0415

    if not om.is_ollama_running(cfg.ollama_url):
        err_console.print(f"[red]Ollama is not reachable at {cfg.ollama_url}.[/red]")
        raise typer.Exit(1)

    try:
        reprocess_document(doc_id, cfg)
        console.print(f"[green]✓[/green] Reprocessed: {doc_id}")
    except FileNotFoundError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc
