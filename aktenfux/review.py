"""Review: list and display documents waiting in the _Review folder."""
from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console
from rich.table import Table

from aktenfux.schema import SidecarDocument
from aktenfux.storage import read_sidecar, sidecar_path_for

logger = logging.getLogger(__name__)

console = Console()

# Number of characters shown for the document ID in the review table.
# Must match _DOC_ID_LENGTH in main.py (16) so the full ID is visible and
# can be copy-pasted directly into `afu approve` / `afu reject`.
_ID_DISPLAY_LENGTH = 16


def list_review_documents(review_path: Path) -> list[SidecarDocument]:
    """Return all sidecar documents found in *review_path*."""
    sidecars: list[SidecarDocument] = []
    if not review_path.exists():
        return sidecars

    for pdf in sorted(review_path.glob("*.pdf")):
        sidecar = read_sidecar(pdf)
        if sidecar is None:
            logger.warning("No sidecar found for %s, skipping.", pdf.name)
            continue
        sidecars.append(sidecar)

    return sidecars


def find_document_by_id(review_path: Path, doc_id: str) -> tuple[Path, SidecarDocument] | None:
    """Find a PDF and its sidecar in *review_path* by document ID.

    *doc_id* is matched against the full stored ID first (exact match), then
    as a prefix, so users can supply the abbreviated ID shown in the review table.
    """
    if not review_path.exists():
        return None

    for pdf in review_path.glob("*.pdf"):
        sidecar = read_sidecar(pdf)
        if sidecar is not None and (
            sidecar.id == doc_id or sidecar.id.startswith(doc_id)
        ):
            return pdf, sidecar

    return None


def print_review_table(sidecars: list[SidecarDocument]) -> None:
    """Render a Rich table of review documents to the console."""
    if not sidecars:
        console.print("[yellow]No documents in _Review.[/yellow]")
        return

    table = Table(title="Documents in Review", show_lines=True)
    table.add_column("ID", style="cyan", no_wrap=True, max_width=16)
    table.add_column("File", style="white", max_width=40)
    table.add_column("Date", style="green", no_wrap=True)
    table.add_column("From", style="magenta", max_width=20)
    table.add_column("Type", style="blue")
    table.add_column("Category", style="blue")
    table.add_column("Summary", max_width=40)
    table.add_column("Action?", style="red")
    table.add_column("Deadline", style="yellow", no_wrap=True)
    table.add_column("Conf.", style="dim")
    table.add_column("Suggested Filename", max_width=40)

    for s in sidecars:
        table.add_row(
            s.id[:_ID_DISPLAY_LENGTH],
            Path(s.current_path).name,
            s.document_date or "",
            s.correspondent or "",
            s.document_type,
            s.category,
            s.summary_short[:60] if s.summary_short else "",
            "✓" if s.action_required else "",
            s.deadline or "",
            f"{s.confidence:.0%}",
            s.suggested_filename or "",
        )

    console.print(table)
