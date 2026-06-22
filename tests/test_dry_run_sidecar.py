"""Tests for dry-run sidecar writing in the processing pipeline."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aktenfuchs.config import AktenfuchsConfig
from aktenfuchs.main import _process_single
from aktenfuchs.schema import DocumentAnalysis, SidecarDocument
from aktenfuchs.storage import sidecar_path_for


def _make_config(base_dir: Path, dry_run: bool = True) -> AktenfuchsConfig:
    return AktenfuchsConfig(
        {
            "base_dir": str(base_dir),
            "dry_run": dry_run,
            "use_sqlite_index": False,
        }
    )


def _fake_analysis() -> DocumentAnalysis:
    return DocumentAnalysis(
        document_date="2026-01-15",
        correspondent="Test GmbH",
        document_type="Invoice",
        topic="Software License",
        category="Invoices",
        confidence=0.85,
        suggested_filename="2026-01-15_Test-GmbH_Invoice_Software-License.pdf",
        suggested_folder="Invoices/Test-GmbH/Software",
        summary_short="Test invoice",
    )


class TestDryRunSidecar:
    def _make_pdf(self, directory: Path, name: str = "scan.pdf") -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        pdf = directory / name
        pdf.write_bytes(b"%PDF-1.4 fake content")
        return pdf

    @patch("aktenfuchs.main.analyze_document")
    @patch("aktenfuchs.main.extract_text", return_value="Some invoice text " * 50)
    def test_dry_run_writes_sidecar_to_dry_run_folder(
        self, mock_extract, mock_analyze, tmp_path
    ):
        mock_analyze.return_value = (_fake_analysis(), [])
        config = _make_config(tmp_path, dry_run=True)
        pdf = self._make_pdf(config.inbox_path)

        _process_single(pdf, config)

        dry_run_dir = config.dry_run_path
        assert dry_run_dir.exists(), "DryRun folder should be created"
        json_files = list(dry_run_dir.glob("*.json"))
        assert len(json_files) == 1, "Exactly one sidecar JSON should be written"

    @patch("aktenfuchs.main.analyze_document")
    @patch("aktenfuchs.main.extract_text", return_value="Some invoice text " * 50)
    def test_dry_run_sidecar_has_dry_run_status(
        self, mock_extract, mock_analyze, tmp_path
    ):
        mock_analyze.return_value = (_fake_analysis(), [])
        config = _make_config(tmp_path, dry_run=True)
        pdf = self._make_pdf(config.inbox_path)

        _process_single(pdf, config)

        json_files = list(config.dry_run_path.glob("*.json"))
        data = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert data["status"] == "dry_run"

    @patch("aktenfuchs.main.analyze_document")
    @patch("aktenfuchs.main.extract_text", return_value="Some invoice text " * 50)
    def test_dry_run_does_not_move_pdf(
        self, mock_extract, mock_analyze, tmp_path
    ):
        mock_analyze.return_value = (_fake_analysis(), [])
        config = _make_config(tmp_path, dry_run=True)
        pdf = self._make_pdf(config.inbox_path)

        _process_single(pdf, config)

        assert pdf.exists(), "PDF must not be moved in dry_run mode"
        assert not any(config.review_path.glob("*.pdf")), "Review folder must stay empty"

    @patch("aktenfuchs.main.analyze_document")
    @patch("aktenfuchs.main.extract_text", return_value="Some invoice text " * 50)
    def test_non_dry_run_does_not_write_to_dry_run_folder(
        self, mock_extract, mock_analyze, tmp_path
    ):
        mock_analyze.return_value = (_fake_analysis(), [])
        config = _make_config(tmp_path, dry_run=False)
        pdf = self._make_pdf(config.inbox_path)

        _process_single(pdf, config)

        if config.dry_run_path.exists():
            assert not any(config.dry_run_path.glob("*.json")), (
                "No sidecar should be written to dry_run folder in non-dry-run mode"
            )
