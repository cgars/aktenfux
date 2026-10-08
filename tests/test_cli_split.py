"""CLI tests for `afu split`."""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter
from typer.testing import CliRunner

from aktenfux.cli import app
from aktenfux.config import AktenfuxConfig
from aktenfux.schema import SidecarDocument
from aktenfux.storage import read_sidecar, sha256_file

runner = CliRunner()


def _make_config(base_dir: Path) -> AktenfuxConfig:
    return AktenfuxConfig(
        {
            "base_dir": str(base_dir),
            "dry_run": False,
            "use_sqlite_index": False,
        }
    )


def _pdf_bytes(page_count: int) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _write_split_doc(split_dir: Path, name: str, doc_id: str, pages: int) -> Path:
    split_dir.mkdir(parents=True, exist_ok=True)
    pdf = split_dir / name
    pdf.write_bytes(_pdf_bytes(pages))
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


def test_split_dry_run_previews_only(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.dry_run = True
    _write_split_doc(cfg.split_path, "dry.pdf", "docsplitcli000001", pages=5)

    with patch("aktenfux.cli._load_config", return_value=cfg):
        result = runner.invoke(
            app,
            ["split", "docsplitcli000001", "--before", "3"],
        )

    assert result.exit_code == 0, result.output
    assert "pages 1-2 -> _Inbox/dry--part-01.pdf" in result.output
    assert "Dry-run" in result.output
    assert not (cfg.inbox_path / "dry--part-01.pdf").exists()


def test_split_cancel_keeps_state_unchanged(tmp_path):
    cfg = _make_config(tmp_path)
    source = _write_split_doc(cfg.split_path, "cancel.pdf", "docsplitcli000002", pages=5)

    with patch("aktenfux.cli._load_config", return_value=cfg):
        result = runner.invoke(
            app,
            ["split", "docsplitcli000002", "--before", "3"],
            input="n\n",
        )

    assert result.exit_code == 0, result.output
    assert "Cancelled." in result.output
    assert not (cfg.inbox_path / "cancel--part-01.pdf").exists()
    sidecar = read_sidecar(source)
    assert sidecar is not None
    assert sidecar.split_operations == []


def test_split_execute_after_confirmation(tmp_path):
    cfg = _make_config(tmp_path)
    _write_split_doc(cfg.split_path, "run.pdf", "docsplitcli000003", pages=6)

    with patch("aktenfux.cli._load_config", return_value=cfg):
        result = runner.invoke(
            app,
            ["split", "docsplitcli000003", "--before", "4"],
            input="y\n",
        )

    assert result.exit_code == 0, result.output
    assert "Split complete" in result.output
    assert (cfg.inbox_path / "run--part-01.pdf").exists()
    assert (cfg.inbox_path / "run--part-02.pdf").exists()
