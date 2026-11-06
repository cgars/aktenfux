"""Tests for the synthetic, inbox-only end-to-end UAT document."""
from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfReader

from scripts.create_e2e_uat_pdf import create_uat_pdf


def test_create_uat_pdf_is_extractable_and_contains_two_documents(tmp_path: Path):
    inbox = tmp_path / "_Inbox"
    inbox.mkdir()
    output = inbox / "synthetic-multi-document.pdf"

    create_uat_pdf(output)

    reader = PdfReader(str(output))
    pages = [page.extract_text() or "" for page in reader.pages]
    assert len(pages) == 6
    assert all("RE-2026-4711" in text for text in pages[:3])
    assert "ENDE DER RECHNUNG" in pages[2]
    assert all("MV-2026-9007" in text for text in pages[3:])
    assert "WALDWEG MOBILITAET AG" in pages[3]
    assert not output.with_suffix(".json").exists()

    with pytest.raises(FileExistsError):
        create_uat_pdf(output)
