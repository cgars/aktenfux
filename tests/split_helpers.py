"""Shared helpers for split tests."""
from __future__ import annotations

import io
from pathlib import Path

from pypdf import PdfWriter

from aktenfux.config import AktenfuxConfig
from aktenfux.schema import SidecarDocument
from aktenfux.storage import sha256_file


def make_config(base_dir: Path) -> AktenfuxConfig:
    return AktenfuxConfig(
        {
            "base_dir": str(base_dir),
            "dry_run": False,
            "use_sqlite_index": False,
        }
    )


def pdf_bytes(page_count: int) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def write_split_doc(split_dir: Path, name: str, doc_id: str, pages: int) -> Path:
    split_dir.mkdir(parents=True, exist_ok=True)
    pdf = split_dir / name
    pdf.write_bytes(pdf_bytes(pages))
    sidecar = SidecarDocument(
        id=doc_id,
        original_path=str(pdf),
        current_path=str(pdf),
        sha256=sha256_file(pdf),
        suggested_filename=name,
        status="approved",
    )
    pdf.with_suffix(".json").write_text(sidecar.model_dump_json(indent=2), encoding="utf-8")
    return pdf
