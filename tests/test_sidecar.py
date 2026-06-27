"""Tests for sidecar JSON read/write operations."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aktenfux.schema import SidecarDocument
from aktenfux.storage import read_sidecar, sidecar_path_for, write_sidecar


def _make_sidecar(doc_id: str = "test001", **kwargs) -> SidecarDocument:
    defaults = {
        "id": doc_id,
        "original_path": "/inbox/scan.pdf",
        "current_path": "/review/2026-01-01_Test_Invoice.pdf",
        "sha256": "c" * 64,
    }
    defaults.update(kwargs)
    return SidecarDocument(**defaults)


class TestWriteSidecar:
    def test_creates_json_file(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"fake pdf")
        sidecar = _make_sidecar(current_path=str(pdf))

        written = write_sidecar(sidecar, pdf)

        assert written == sidecar_path_for(pdf)
        assert written.exists()
        assert written.suffix == ".json"

    def test_json_content_is_valid(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"fake pdf")
        sidecar = _make_sidecar(
            doc_id="myid",
            correspondent="Acme Corp",
            document_type="Contract",
        )

        write_sidecar(sidecar, pdf)
        content = json.loads(sidecar_path_for(pdf).read_text(encoding="utf-8"))

        assert content["id"] == "myid"
        assert content["correspondent"] == "Acme Corp"
        assert content["document_type"] == "Contract"

    def test_overwrites_existing_sidecar(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"fake pdf")

        sidecar1 = _make_sidecar(correspondent="First")
        write_sidecar(sidecar1, pdf)

        sidecar2 = _make_sidecar(correspondent="Second")
        write_sidecar(sidecar2, pdf)

        content = json.loads(sidecar_path_for(pdf).read_text(encoding="utf-8"))
        assert content["correspondent"] == "Second"

    def test_unicode_content_preserved(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"fake pdf")
        sidecar = _make_sidecar(correspondent="Müller & Söhne GmbH")

        write_sidecar(sidecar, pdf)
        content = json.loads(sidecar_path_for(pdf).read_text(encoding="utf-8"))
        assert content["correspondent"] == "Müller & Söhne GmbH"


class TestReadSidecar:
    def test_reads_written_sidecar(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"fake pdf")
        sidecar = _make_sidecar(
            doc_id="readtest",
            correspondent="Test GmbH",
            confidence=0.88,
        )
        write_sidecar(sidecar, pdf)

        restored = read_sidecar(pdf)

        assert restored is not None
        assert restored.id == "readtest"
        assert restored.correspondent == "Test GmbH"
        assert restored.confidence == 0.88

    def test_missing_sidecar_returns_none(self, tmp_path):
        pdf = tmp_path / "missing.pdf"
        assert read_sidecar(pdf) is None

    def test_corrupted_sidecar_returns_none(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"fake pdf")
        sidecar_path_for(pdf).write_text("NOT VALID JSON {{{{", encoding="utf-8")

        result = read_sidecar(pdf)
        assert result is None

    def test_roundtrip_preserves_all_fields(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"fake pdf")
        sidecar = _make_sidecar(
            doc_id="roundtrip",
            document_date="2026-06-20",
            correspondent="HUK-COBURG",
            document_type="Invoice",
            category="Insurance",
            topic="Car Insurance",
            tags=["car", "insurance"],
            summary_short="Car insurance invoice.",
            action_required=True,
            action_summary="Check direct debit.",
            deadline="2026-07-01",
            suggested_folder="Insurance/HUK-COBURG/Car",
            suggested_filename="2026-06-20_HUK-COBURG_Invoice_Car.pdf",
            confidence=0.86,
            model="qwen3:8b",
            warnings=["test warning"],
        )

        write_sidecar(sidecar, pdf)
        restored = read_sidecar(pdf)

        assert restored is not None
        assert restored.document_date == "2026-06-20"
        assert restored.correspondent == "HUK-COBURG"
        assert restored.document_type == "Invoice"
        assert restored.category == "Insurance"
        assert "car" in restored.tags
        assert restored.action_required is True
        assert restored.deadline == "2026-07-01"
        assert restored.confidence == 0.86
        assert "test warning" in restored.warnings


class TestSidecarPathFor:
    def test_pdf_gets_json_sidecar(self, tmp_path):
        pdf = tmp_path / "document.pdf"
        result = sidecar_path_for(pdf)
        assert result == tmp_path / "document.json"

    def test_preserves_full_stem(self, tmp_path):
        pdf = tmp_path / "2026-06-20_HUK-COBURG_Invoice_Car-Insurance.pdf"
        result = sidecar_path_for(pdf)
        assert result.stem == "2026-06-20_HUK-COBURG_Invoice_Car-Insurance"
        assert result.suffix == ".json"
