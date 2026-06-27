"""Tests for aktenfux/review.py – document listing and ID lookup."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aktenfux.review import find_document_by_id, list_review_documents
from aktenfux.schema import SidecarDocument


def _write_sidecar(directory: Path, pdf_name: str, doc_id: str) -> Path:
    """Create a fake PDF and its sidecar JSON in *directory*."""
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
    json_path = pdf.with_suffix(".json")
    json_path.write_text(
        sidecar_data.model_dump_json(indent=2), encoding="utf-8"
    )
    return pdf


class TestFindDocumentById:
    def test_exact_match(self, tmp_path):
        review_dir = tmp_path / "_Review"
        _write_sidecar(review_dir, "doc.pdf", "abcdef1234567890")

        result = find_document_by_id(review_dir, "abcdef1234567890")
        assert result is not None
        pdf, sidecar = result
        assert sidecar.id == "abcdef1234567890"

    def test_prefix_match(self, tmp_path):
        """A prefix of the full ID must also find the document."""
        review_dir = tmp_path / "_Review"
        _write_sidecar(review_dir, "doc.pdf", "abcdef1234567890")

        # Simulate user copy-pasting the 14-char truncated display from old table
        result = find_document_by_id(review_dir, "abcdef12345678")
        assert result is not None
        _, sidecar = result
        assert sidecar.id == "abcdef1234567890"

    def test_no_match_returns_none(self, tmp_path):
        review_dir = tmp_path / "_Review"
        _write_sidecar(review_dir, "doc.pdf", "abcdef1234567890")

        result = find_document_by_id(review_dir, "000000")
        assert result is None

    def test_nonexistent_dir_returns_none(self, tmp_path):
        result = find_document_by_id(tmp_path / "no_such_dir", "abc123")
        assert result is None

    def test_multiple_docs_returns_correct_one(self, tmp_path):
        review_dir = tmp_path / "_Review"
        _write_sidecar(review_dir, "doc1.pdf", "aaaa1111bbbb2222")
        _write_sidecar(review_dir, "doc2.pdf", "cccc3333dddd4444")

        result = find_document_by_id(review_dir, "cccc3333dddd4444")
        assert result is not None
        _, sidecar = result
        assert sidecar.id == "cccc3333dddd4444"

    def test_prefix_does_not_match_wrong_doc(self, tmp_path):
        review_dir = tmp_path / "_Review"
        _write_sidecar(review_dir, "doc1.pdf", "aaaa1111bbbb2222")
        _write_sidecar(review_dir, "doc2.pdf", "cccc3333dddd4444")

        # Prefix "aaaa" should match doc1, not doc2
        result = find_document_by_id(review_dir, "aaaa")
        assert result is not None
        _, sidecar = result
        assert sidecar.id == "aaaa1111bbbb2222"


class TestIdDisplayLength:
    def test_full_id_is_shown_in_table(self):
        """_ID_DISPLAY_LENGTH must equal the full ID length used in main.py."""
        from aktenfux.review import _ID_DISPLAY_LENGTH

        # _DOC_ID_LENGTH in main.py is 16; the table must show all 16 chars.
        assert _ID_DISPLAY_LENGTH == 16, (
            "_ID_DISPLAY_LENGTH must match _DOC_ID_LENGTH (16) so the full "
            "ID is visible in the review table and can be used with approve/reject"
        )


class TestListReviewDocuments:
    def test_empty_dir_returns_empty_list(self, tmp_path):
        review_dir = tmp_path / "_Review"
        review_dir.mkdir()
        assert list_review_documents(review_dir) == []

    def test_nonexistent_dir_returns_empty_list(self, tmp_path):
        assert list_review_documents(tmp_path / "no_such") == []

    def test_returns_sidecar_for_each_pdf(self, tmp_path):
        review_dir = tmp_path / "_Review"
        _write_sidecar(review_dir, "a.pdf", "aaaa000000000001")
        _write_sidecar(review_dir, "b.pdf", "bbbb000000000002")

        docs = list_review_documents(review_dir)
        assert len(docs) == 2
        ids = {d.id for d in docs}
        assert ids == {"aaaa000000000001", "bbbb000000000002"}

    def test_skips_pdf_without_sidecar(self, tmp_path):
        review_dir = tmp_path / "_Review"
        review_dir.mkdir()
        # PDF with no matching JSON
        (review_dir / "orphan.pdf").write_bytes(b"%PDF-1.4 fake")
        # PDF with sidecar
        _write_sidecar(review_dir, "good.pdf", "cccc000000000003")

        docs = list_review_documents(review_dir)
        assert len(docs) == 1
        assert docs[0].id == "cccc000000000003"
