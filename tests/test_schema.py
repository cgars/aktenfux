"""Tests for aktenfuchs/schema.py."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from aktenfuchs.schema import Amount, DocumentAnalysis, Entities, SidecarDocument


class TestAmount:
    def test_basic(self):
        a = Amount(label="total", amount=99.99, currency="EUR")
        assert a.amount == 99.99
        assert a.currency == "EUR"

    def test_default_currency(self):
        a = Amount(label="total", amount=10.0)
        assert a.currency == "EUR"


class TestEntities:
    def test_default_empty(self):
        e = Entities()
        assert e.people == []
        assert e.organizations == []

    def test_with_data(self):
        e = Entities(organizations=["Acme Corp"], contract_numbers=["CN-001"])
        assert "Acme Corp" in e.organizations
        assert "CN-001" in e.contract_numbers


class TestDocumentAnalysis:
    def test_minimal_valid(self):
        da = DocumentAnalysis()
        assert da.document_type == "Other"
        assert da.category == "Other"
        assert da.confidence == 0.0
        assert da.action_required is False

    def test_full_valid(self):
        data = {
            "document_date": "2026-06-20",
            "correspondent": "HUK-COBURG",
            "document_type": "Invoice",
            "topic": "Car Insurance",
            "category": "Insurance",
            "tags": ["car", "insurance"],
            "summary_short": "HUK-COBURG invoices the car insurance premium.",
            "summary": "Longer summary here.",
            "key_points": ["Point 1", "Point 2"],
            "action_required": True,
            "action_summary": "Pay the invoice.",
            "deadline": "2026-07-01",
            "amounts": [{"label": "premium", "amount": 347.82, "currency": "EUR"}],
            "entities": {"organizations": ["HUK-COBURG"]},
            "suggested_folder": "Insurance/HUK-COBURG/Car",
            "suggested_filename": "2026-06-20_HUK-COBURG_Invoice_Car-Insurance.pdf",
            "confidence": 0.86,
        }
        da = DocumentAnalysis.model_validate(data)
        assert da.correspondent == "HUK-COBURG"
        assert da.confidence == 0.86
        assert len(da.amounts) == 1

    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            DocumentAnalysis(confidence=1.5)
        with pytest.raises(ValidationError):
            DocumentAnalysis(confidence=-0.1)

    def test_key_points_limited_to_5(self):
        da = DocumentAnalysis(key_points=["a", "b", "c", "d", "e", "f", "g"])
        assert len(da.key_points) == 5

    def test_action_summary_cleared_when_not_required(self):
        da = DocumentAnalysis(action_required=False, action_summary="Do something")
        assert da.action_summary is None

    def test_action_summary_kept_when_required(self):
        da = DocumentAnalysis(action_required=True, action_summary="Pay invoice")
        assert da.action_summary == "Pay invoice"

    def test_empty_string_date_becomes_none(self):
        da = DocumentAnalysis(document_date="")
        assert da.document_date is None

    def test_null_string_date_becomes_none(self):
        da = DocumentAnalysis(document_date="null")
        assert da.document_date is None

    def test_invalid_document_type(self):
        with pytest.raises(ValidationError):
            DocumentAnalysis(document_type="Nonsense")

    def test_summary_short_filled_from_summary_when_empty(self):
        """summary_short must be auto-filled from summary when the LLM omits it."""
        da = DocumentAnalysis(summary="A longer description of the document.", summary_short="")
        assert da.summary_short == "A longer description of the document."

    def test_summary_short_truncated_at_120_chars_when_filled_from_summary(self):
        long_summary = "x" * 200
        da = DocumentAnalysis(summary=long_summary, summary_short="")
        assert len(da.summary_short) == 120

    def test_summary_short_not_overwritten_when_provided(self):
        """An explicitly provided summary_short must not be overwritten."""
        da = DocumentAnalysis(
            summary_short="Short desc.",
            summary="Much longer summary text that should not replace the short one.",
        )
        assert da.summary_short == "Short desc."

    def test_summary_short_stays_empty_when_no_summary(self):
        """When both summary and summary_short are empty, summary_short remains empty."""
        da = DocumentAnalysis(summary_short="", summary="")
        assert da.summary_short == ""

    def test_json_roundtrip(self):
        da = DocumentAnalysis(
            document_date="2026-01-01",
            correspondent="Test Corp",
            document_type="Invoice",
            confidence=0.9,
        )
        serialized = da.model_dump_json()
        restored = DocumentAnalysis.model_validate_json(serialized)
        assert restored.correspondent == da.correspondent
        assert restored.confidence == da.confidence


class TestSidecarDocument:
    def _make_sidecar(self, **kwargs) -> SidecarDocument:
        defaults = {
            "id": "abc123",
            "original_path": "/inbox/scan001.pdf",
            "current_path": "/review/2026-01-01_Test_Invoice.pdf",
            "sha256": "a" * 64,
        }
        defaults.update(kwargs)
        return SidecarDocument(**defaults)

    def test_minimal(self):
        s = self._make_sidecar()
        assert s.id == "abc123"
        assert s.status == "review"
        assert s.warnings == []

    def test_from_analysis(self):
        analysis = DocumentAnalysis(
            document_date="2026-06-20",
            correspondent="Test GmbH",
            document_type="Invoice",
            category="Invoices",
            topic="Water bill",
            summary_short="Monthly water bill.",
            confidence=0.75,
            suggested_filename="2026-06-20_Test_Invoice_Water.pdf",
            suggested_folder="Invoices/Test-GmbH",
        )
        sidecar = SidecarDocument.from_analysis(
            analysis,
            doc_id="id001",
            original_path="/inbox/scan.pdf",
            current_path="/review/2026-06-20_Test_Invoice_Water.pdf",
            sha256="b" * 64,
            model="qwen3:8b",
            warnings=["test warning"],
        )
        assert sidecar.id == "id001"
        assert sidecar.correspondent == "Test GmbH"
        assert sidecar.model == "qwen3:8b"
        assert "test warning" in sidecar.warnings
        assert sidecar.status == "review"

    def test_json_serialization(self):
        s = self._make_sidecar(
            correspondent="ACME",
            document_type="Contract",
            confidence=0.8,
        )
        json_str = s.model_dump_json(indent=2)
        data = json.loads(json_str)
        assert data["correspondent"] == "ACME"
        assert data["confidence"] == 0.8

    def test_json_roundtrip(self):
        s = self._make_sidecar(
            document_date="2026-03-15",
            tags=["test", "roundtrip"],
            amounts=[{"label": "total", "amount": 123.45, "currency": "EUR"}],
        )
        restored = SidecarDocument.model_validate_json(s.model_dump_json())
        assert restored.document_date == "2026-03-15"
        assert restored.tags == ["test", "roundtrip"]
        assert restored.amounts[0].amount == 123.45
