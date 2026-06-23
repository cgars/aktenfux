"""File storage helpers: hashing, moving, path validation, sidecar I/O."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path

from aktenfux.schema import SidecarDocument

logger = logging.getLogger(__name__)

_SIDECAR_SUFFIX = ".json"
_MARKDOWN_SUFFIX = ".md"


# ---------------------------------------------------------------------------
# SHA-256
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    """Return the lowercase hex SHA-256 of *path*."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def assert_within_base(path: Path, base_dir: Path) -> None:
    """Raise ValueError if *path* is not inside *base_dir*."""
    try:
        path.resolve().relative_to(base_dir.resolve())
    except ValueError:
        raise ValueError(
            f"Path traversal detected: {path} is not inside {base_dir}"
        )


# ---------------------------------------------------------------------------
# Sidecar JSON helpers
# ---------------------------------------------------------------------------


def sidecar_path_for(pdf_path: Path) -> Path:
    """Return the sidecar JSON path for a given PDF path."""
    return pdf_path.with_suffix(_SIDECAR_SUFFIX)


def markdown_path_for(pdf_path: Path) -> Path:
    """Return the Markdown summary path for a given PDF path."""
    return pdf_path.with_suffix(_MARKDOWN_SUFFIX)


def write_sidecar(sidecar: SidecarDocument, pdf_path: Path) -> Path:
    """Write *sidecar* as JSON next to *pdf_path*. Returns the sidecar path."""
    dest = sidecar_path_for(pdf_path)
    dest.write_text(
        sidecar.model_dump_json(indent=2),
        encoding="utf-8",
    )
    logger.debug("Sidecar written: %s", dest)
    return dest


def read_sidecar(pdf_path: Path) -> SidecarDocument | None:
    """Read and parse the sidecar JSON for *pdf_path*. Returns None if missing."""
    src = sidecar_path_for(pdf_path)
    if not src.exists():
        return None
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
        return SidecarDocument.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not parse sidecar %s: %s", src, exc)
        return None


def write_markdown_summary(sidecar: SidecarDocument, pdf_path: Path) -> Path:
    """Write a human-readable Markdown summary next to *pdf_path*."""
    dest = markdown_path_for(pdf_path)
    lines = [
        f"# {sidecar.topic or 'Document'}",
        "",
        f"**Date:** {sidecar.document_date or 'unknown'}",
        f"**From:** {sidecar.correspondent or 'Unknown'}",
        f"**Type:** {sidecar.document_type}",
        f"**Category:** {sidecar.category}",
        "",
        "## Summary",
        "",
        sidecar.summary or sidecar.summary_short,
        "",
    ]
    if sidecar.key_points:
        lines += ["## Key Points", ""]
        for point in sidecar.key_points:
            lines.append(f"- {point}")
        lines.append("")
    if sidecar.action_required and sidecar.action_summary:
        lines += [
            "## Action Required",
            "",
            sidecar.action_summary,
            "",
        ]
    if sidecar.deadline:
        lines.append(f"**Deadline:** {sidecar.deadline}")
    dest.write_text("\n".join(lines), encoding="utf-8")
    logger.debug("Markdown summary written: %s", dest)
    return dest


# ---------------------------------------------------------------------------
# File movement helpers
# ---------------------------------------------------------------------------


def move_file_with_sidecar(
    source_pdf: Path,
    dest_pdf: Path,
    *,
    base_dir: Path,
    dry_run: bool = False,
    move_markdown: bool = False,
) -> None:
    """Move *source_pdf* (and optionally its sidecar + markdown) to *dest_pdf*.

    Safety checks:
    - Both paths must be within *base_dir*.
    - Never overwrite existing files.
    """
    assert_within_base(source_pdf, base_dir)
    assert_within_base(dest_pdf, base_dir)

    if dest_pdf.exists():
        raise FileExistsError(f"Target already exists: {dest_pdf}")

    source_json = sidecar_path_for(source_pdf)
    dest_json = sidecar_path_for(dest_pdf)

    source_md = markdown_path_for(source_pdf)
    dest_md = markdown_path_for(dest_pdf)

    if dry_run:
        logger.info("[DRY-RUN] Would move %s → %s", source_pdf, dest_pdf)
        if source_json.exists():
            logger.info("[DRY-RUN] Would move %s → %s", source_json, dest_json)
        if move_markdown and source_md.exists():
            logger.info("[DRY-RUN] Would move %s → %s", source_md, dest_md)
        return

    dest_pdf.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source_pdf), str(dest_pdf))
    logger.info("Moved %s → %s", source_pdf, dest_pdf)

    if source_json.exists():
        shutil.move(str(source_json), str(dest_json))
        logger.debug("Moved sidecar %s → %s", source_json, dest_json)

    if move_markdown and source_md.exists():
        shutil.move(str(source_md), str(dest_md))
        logger.debug("Moved markdown %s → %s", source_md, dest_md)
