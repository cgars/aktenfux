"""Tests for PDF metadata writing (pdf_metadata module)."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from aktenfux.pdf_metadata import SUBJECT_MAX_CHARS, build_pdf_metadata, write_pdf_metadata
from aktenfux.schema import SidecarDocument


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_pdf_bytes() -> bytes:
    """Return a minimal valid one-page PDF as bytes."""
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_sidecar(**kwargs) -> SidecarDocument:
    defaults: dict = {
        "id": "abc123",
        "original_path": "/inbox/scan.pdf",
        "current_path": "/review/scan.pdf",
        "sha256": "a" * 64,
    }
    defaults.update(kwargs)
    return SidecarDocument(**defaults)


def _read_metadata(pdf_path: Path) -> dict:
    reader = PdfReader(str(pdf_path))
    return dict(reader.metadata or {})


# ---------------------------------------------------------------------------
# build_pdf_metadata
# ---------------------------------------------------------------------------


class TestBuildPdfMetadata:
    def test_title_from_topic(self):
        sidecar = _make_sidecar(topic="Invoice from Acme")
        meta = build_pdf_metadata(sidecar)
        assert meta["/Title"] == "Invoice from Acme"

    def test_author_from_correspondent(self):
        sidecar = _make_sidecar(correspondent="Acme Corp")
        meta = build_pdf_metadata(sidecar)
        assert meta["/Author"] == "Acme Corp"

    def test_subject_from_summary_short(self):
        sidecar = _make_sidecar(summary_short="A short description of the document.")
        meta = build_pdf_metadata(sidecar)
        assert meta["/Subject"] == "A short description of the document."

    def test_subject_truncated_to_max(self):
        long_text = "x" * (SUBJECT_MAX_CHARS + 50)
        sidecar = _make_sidecar(summary_short=long_text)
        meta = build_pdf_metadata(sidecar)
        assert meta["/Subject"] == "x" * SUBJECT_MAX_CHARS

    def test_keywords_from_tags(self):
        sidecar = _make_sidecar(tags=["invoice", "acme", "2026"])
        meta = build_pdf_metadata(sidecar)
        assert meta["/Keywords"] == "invoice, acme, 2026"

    def test_creator_always_aktenfux(self):
        sidecar = _make_sidecar()
        meta = build_pdf_metadata(sidecar)
        assert meta["/Creator"] == "Aktenfux"

    def test_producer_always_aktenfux(self):
        sidecar = _make_sidecar()
        meta = build_pdf_metadata(sidecar)
        assert meta["/Producer"] == "Aktenfux"

    def test_missing_fields_omitted(self):
        sidecar = _make_sidecar(topic="", correspondent=None, summary_short="", tags=[])
        meta = build_pdf_metadata(sidecar)
        assert "/Title" not in meta
        assert "/Author" not in meta
        assert "/Subject" not in meta
        assert "/Keywords" not in meta

    def test_whitespace_only_fields_omitted(self):
        sidecar = _make_sidecar(topic="   ", correspondent="  ")
        meta = build_pdf_metadata(sidecar)
        assert "/Title" not in meta
        assert "/Author" not in meta

    def test_full_sidecar_populates_all_standard_fields(self):
        sidecar = _make_sidecar(
            topic="Electricity bill",
            correspondent="City Power",
            summary_short="Monthly electricity invoice for March 2026.",
            tags=["electricity", "invoice", "utilities"],
        )
        meta = build_pdf_metadata(sidecar)
        assert "/Title" in meta
        assert "/Author" in meta
        assert "/Subject" in meta
        assert "/Keywords" in meta
        assert "/Creator" in meta
        assert "/Producer" in meta


# ---------------------------------------------------------------------------
# write_pdf_metadata
# ---------------------------------------------------------------------------


class TestWritePdfMetadata:
    def test_writes_title_into_pdf(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(_minimal_pdf_bytes())
        sidecar = _make_sidecar(topic="My Document Title")

        write_pdf_metadata(pdf, sidecar)

        assert _read_metadata(pdf).get("/Title") == "My Document Title"

    def test_writes_author_into_pdf(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(_minimal_pdf_bytes())
        sidecar = _make_sidecar(correspondent="Jane Doe")

        write_pdf_metadata(pdf, sidecar)

        assert _read_metadata(pdf).get("/Author") == "Jane Doe"

    def test_writes_subject_into_pdf(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(_minimal_pdf_bytes())
        sidecar = _make_sidecar(summary_short="Short doc description.")

        write_pdf_metadata(pdf, sidecar)

        assert _read_metadata(pdf).get("/Subject") == "Short doc description."

    def test_writes_keywords_into_pdf(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(_minimal_pdf_bytes())
        sidecar = _make_sidecar(tags=["alpha", "beta"])

        write_pdf_metadata(pdf, sidecar)

        assert _read_metadata(pdf).get("/Keywords") == "alpha, beta"

    def test_writes_creator_and_producer(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(_minimal_pdf_bytes())
        sidecar = _make_sidecar()

        write_pdf_metadata(pdf, sidecar)

        meta = _read_metadata(pdf)
        assert meta.get("/Creator") == "Aktenfux"
        assert meta.get("/Producer") == "Aktenfux"

    def test_pdf_content_preserved(self, tmp_path):
        """The page count must be unchanged after writing metadata."""
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(_minimal_pdf_bytes())
        sidecar = _make_sidecar(topic="Preserve pages test")

        write_pdf_metadata(pdf, sidecar)

        reader = PdfReader(str(pdf))
        assert len(reader.pages) == 1

    def test_updates_existing_pdf_in_place(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(_minimal_pdf_bytes())
        sidecar = _make_sidecar(topic="First pass")

        write_pdf_metadata(pdf, sidecar)
        assert _read_metadata(pdf).get("/Title") == "First pass"

        sidecar2 = _make_sidecar(topic="Second pass")
        write_pdf_metadata(pdf, sidecar2)
        assert _read_metadata(pdf).get("/Title") == "Second pass"

    def test_raises_if_pdf_not_found(self, tmp_path):
        missing = tmp_path / "missing.pdf"
        sidecar = _make_sidecar()

        with pytest.raises(FileNotFoundError):
            write_pdf_metadata(missing, sidecar)

    def test_no_tmp_file_left_behind(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(_minimal_pdf_bytes())
        sidecar = _make_sidecar(topic="Cleanup test")

        write_pdf_metadata(pdf, sidecar)

        assert not (tmp_path / "doc.tmp_meta").exists()

    def test_empty_sidecar_still_writes_creator_producer(self, tmp_path):
        """Even with no document data, Creator and Producer must be set."""
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(_minimal_pdf_bytes())
        sidecar = _make_sidecar(topic="", correspondent=None, summary_short="", tags=[])

        write_pdf_metadata(pdf, sidecar)

        meta = _read_metadata(pdf)
        assert meta.get("/Creator") == "Aktenfux"
        assert meta.get("/Producer") == "Aktenfux"

    def test_full_example_all_fields(self, tmp_path):
        """Integration-style test: all fields set, all appear in the PDF."""
        pdf = tmp_path / "invoice.pdf"
        pdf.write_bytes(_minimal_pdf_bytes())
        sidecar = _make_sidecar(
            topic="Electricity Bill March 2026",
            correspondent="City Power GmbH",
            summary_short="Electricity invoice for March 2026, 94.50 EUR.",
            tags=["electricity", "utility", "invoice"],
            document_type="Invoice",
            category="Home",
        )

        write_pdf_metadata(pdf, sidecar)

        meta = _read_metadata(pdf)
        assert meta["/Title"] == "Electricity Bill March 2026"
        assert meta["/Author"] == "City Power GmbH"
        assert meta["/Subject"] == "Electricity invoice for March 2026, 94.50 EUR."
        assert meta["/Keywords"] == "electricity, utility, invoice"
        assert meta["/Creator"] == "Aktenfux"
        assert meta["/Producer"] == "Aktenfux"
