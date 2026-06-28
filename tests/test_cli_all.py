"""Tests for the --all flag on the approve and reject CLI commands."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from aktenfux.cli import app
from aktenfux.config import AktenfuxConfig
from aktenfux.schema import SidecarDocument

runner = CliRunner()


def _make_config(base_dir: Path) -> AktenfuxConfig:
    """Return a non-dry-run config rooted at *base_dir*."""
    return AktenfuxConfig(
        {
            "base_dir": str(base_dir),
            "dry_run": False,
            "use_sqlite_index": False,
        }
    )


def _write_sidecar(directory: Path, pdf_name: str, doc_id: str) -> Path:
    """Create a minimal PDF and its sidecar JSON in *directory*."""
    directory.mkdir(parents=True, exist_ok=True)
    pdf = directory / pdf_name
    pdf.write_bytes(b"%PDF-1.4 fake")

    sidecar_data = SidecarDocument(
        id=doc_id,
        original_path=str(pdf),
        current_path=str(pdf),
        sha256="a" * 64,
        suggested_filename=pdf_name,
        status="review",
    )
    pdf.with_suffix(".json").write_text(
        sidecar_data.model_dump_json(indent=2), encoding="utf-8"
    )
    return pdf


# ---------------------------------------------------------------------------
# approve --all
# ---------------------------------------------------------------------------


class TestApproveAll:
    def test_approves_all_documents(self, tmp_path):
        cfg = _make_config(tmp_path)
        review_dir = cfg.review_path
        _write_sidecar(review_dir, "doc1.pdf", "aaaa000000000001")
        _write_sidecar(review_dir, "doc2.pdf", "bbbb000000000002")

        with patch("aktenfux.cli._load_config", return_value=cfg):
            result = runner.invoke(app, ["approve", "--all"])

        assert result.exit_code == 0, result.output
        assert "aaaa000000000001" in result.output
        assert "bbbb000000000002" in result.output
        # Documents should have moved out of _Review
        assert not (review_dir / "doc1.pdf").exists()
        assert not (review_dir / "doc2.pdf").exists()

    def test_empty_review_prints_message(self, tmp_path):
        cfg = _make_config(tmp_path)
        cfg.review_path.mkdir(parents=True, exist_ok=True)

        with patch("aktenfux.cli._load_config", return_value=cfg):
            result = runner.invoke(app, ["approve", "--all"])

        assert result.exit_code == 0
        assert "No documents" in result.output

    def test_dry_run_does_not_move_files(self, tmp_path):
        cfg = _make_config(tmp_path)
        cfg.dry_run = True
        review_dir = cfg.review_path
        _write_sidecar(review_dir, "doc1.pdf", "cccc000000000003")

        with patch("aktenfux.cli._load_config", return_value=cfg):
            result = runner.invoke(app, ["approve", "--all"])

        assert result.exit_code == 0
        # In dry-run the file must still be in _Review
        assert (review_dir / "doc1.pdf").exists()
        # Dry-run should still report which document would be processed
        assert "cccc000000000003" in result.output


# ---------------------------------------------------------------------------
# reject --all
# ---------------------------------------------------------------------------


class TestRejectAll:
    def test_rejects_all_documents(self, tmp_path):
        cfg = _make_config(tmp_path)
        review_dir = cfg.review_path
        _write_sidecar(review_dir, "doc1.pdf", "dddd000000000004")
        _write_sidecar(review_dir, "doc2.pdf", "eeee000000000005")

        with patch("aktenfux.cli._load_config", return_value=cfg):
            result = runner.invoke(app, ["reject", "--all"])

        assert result.exit_code == 0, result.output
        assert "dddd000000000004" in result.output
        assert "eeee000000000005" in result.output
        # Documents should have moved out of _Review
        assert not (review_dir / "doc1.pdf").exists()
        assert not (review_dir / "doc2.pdf").exists()
        # Documents should be in _Error
        assert (cfg.error_path / "doc1.pdf").exists()
        assert (cfg.error_path / "doc2.pdf").exists()

    def test_empty_review_prints_message(self, tmp_path):
        cfg = _make_config(tmp_path)
        cfg.review_path.mkdir(parents=True, exist_ok=True)

        with patch("aktenfux.cli._load_config", return_value=cfg):
            result = runner.invoke(app, ["reject", "--all"])

        assert result.exit_code == 0
        assert "No documents" in result.output

    def test_dry_run_does_not_move_files(self, tmp_path):
        cfg = _make_config(tmp_path)
        cfg.dry_run = True
        review_dir = cfg.review_path
        _write_sidecar(review_dir, "doc1.pdf", "ffff000000000006")

        with patch("aktenfux.cli._load_config", return_value=cfg):
            result = runner.invoke(app, ["reject", "--all"])

        assert result.exit_code == 0
        # In dry-run the file must still be in _Review
        assert (review_dir / "doc1.pdf").exists()
        # Dry-run should still report which document would be processed
        assert "ffff000000000006" in result.output


# ---------------------------------------------------------------------------
# Missing doc_id and no --all → error
# ---------------------------------------------------------------------------


class TestMissingArgument:
    def test_approve_no_id_no_all_exits_with_error(self, tmp_path):
        cfg = _make_config(tmp_path)

        with patch("aktenfux.cli._load_config", return_value=cfg):
            result = runner.invoke(app, ["approve"])

        assert result.exit_code != 0

    def test_reject_no_id_no_all_exits_with_error(self, tmp_path):
        cfg = _make_config(tmp_path)

        with patch("aktenfux.cli._load_config", return_value=cfg):
            result = runner.invoke(app, ["reject"])

        assert result.exit_code != 0
