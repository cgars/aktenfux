"""Tests for aktenfux/schema.py."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from aktenfux.schema import (
    DESCRIPTION_SHORT_MAX_CHARS,
    Amount,
    DocumentAnalysis,
    DocumentIntegrity,
    Entities,
    SidecarDocument,
)


VALID_INTEGRITY = {
    "possible_multi_document_scan": False,
    "suspected_document_count": 1,
    "confidence": 0.91,
    "reason": "The document appears to have one consistent sender and topic.",
    "recommended_action": "none",
}


class TestAmount:
    def test_basic(self):
        a = Amount(label="total", amount=99.99, currency="EUR")
        assert a.amount == 99.99
        assert a.currency == "EUR"

    def test_default_currency(self):
        a = Amount(label="total", amount=10.0)
        assert a.currency == "EUR"

    def test_german_comma_decimal(self):
        """German '500,00' should be parsed as 500.0."""
        a = Amount(label="total", amount="500,00")
        assert a.amount == pytest.approx(500.0)

    def test_german_dot_thousands_comma_decimal(self):
        """German '1.500,00' should be parsed as 1500.0."""
        a = Amount(label="total", amount="1.500,00")
        assert a.amount == pytest.approx(1500.0)

    def test_amount_with_currency_symbol(self):
        """Embedded currency symbols should be stripped."""
        a = Amount(label="total", amount="€ 347,82")
        assert a.amount == pytest.approx(347.82)

    def test_amount_plain_float_string(self):
        a = Amount(label="total", amount="123.45")
        assert a.amount == pytest.approx(123.45)

    def test_amount_empty_string_becomes_zero(self):
        a = Amount(label="total", amount="")
        assert a.amount == 0.0


class TestEntities:
    def test_default_empty(self):
        e = Entities()
        assert e.people == []
        assert e.organizations == []

    def test_with_data(self):
        e = Entities(organizations=["Acme Corp"], contract_numbers=["CN-001"])
        assert "Acme Corp" in e.organizations
        assert "CN-001" in e.contract_numbers

    def test_entity_string_coerced_to_list(self):
        """A comma-separated string should be split into a list."""
        e = Entities(organizations="Acme Corp, Beta GmbH")
        assert e.organizations == ["Acme Corp", "Beta GmbH"]

    def test_entity_empty_string_becomes_empty_list(self):
        e = Entities(people="")
        assert e.people == []

    def test_entity_non_list_non_string_becomes_empty_list(self):
        e = Entities(organizations=None)
        assert e.organizations == []


class TestDocumentAnalysis:
    def test_minimal_valid(self):
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, )
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
            "document_integrity": VALID_INTEGRITY,
        }
        da = DocumentAnalysis.model_validate(data)
        assert da.correspondent == "HUK-COBURG"
        assert da.confidence == 0.86
        assert len(da.amounts) == 1

    def test_confidence_bounds(self):
        # Values > 1.0 are interpreted as percentages and normalized, not rejected.
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, confidence=85)
        assert da.confidence == pytest.approx(0.85)
        da2 = DocumentAnalysis(document_integrity=VALID_INTEGRITY, confidence=1.5)  # 1.5 (as percentage) → 1.5/100 → 0.015
        assert da2.confidence == pytest.approx(0.015)
        # Values above 100% are clamped to 1.0.
        da3 = DocumentAnalysis(document_integrity=VALID_INTEGRITY, confidence=150)
        assert da3.confidence == pytest.approx(1.0)
        # Negative values are still rejected.
        with pytest.raises(ValidationError):
            DocumentAnalysis(document_integrity=VALID_INTEGRITY, confidence=-0.1)

    def test_key_points_limited_to_5(self):
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, key_points=["a", "b", "c", "d", "e", "f", "g"])
        assert len(da.key_points) == 5

    def test_action_summary_cleared_when_not_required(self):
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, action_required=False, action_summary="Do something")
        assert da.action_summary is None

    def test_action_summary_kept_when_required(self):
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, action_required=True, action_summary="Pay invoice")
        assert da.action_summary == "Pay invoice"

    def test_empty_string_date_becomes_none(self):
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, document_date="")
        assert da.document_date is None

    def test_null_string_date_becomes_none(self):
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, document_date="null")
        assert da.document_date is None

    def test_invalid_document_type(self):
        # Unknown document types fall back to "Other" instead of raising.
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, document_type="Nonsense")
        assert da.document_type == "Other"

    def test_summary_short_filled_from_summary_when_empty(self):
        """summary_short must be auto-filled from summary when the LLM omits it."""
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, summary="A longer description of the document.", summary_short="")
        assert da.summary_short == "A longer description of the document."

    def test_summary_short_truncated_at_120_chars_when_filled_from_summary(self):
        long_summary = "x" * 200
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, summary=long_summary, summary_short="")
        assert da.summary_short == long_summary[:DESCRIPTION_SHORT_MAX_CHARS]

    def test_summary_short_rstrips_trailing_whitespace_from_summary(self):
        """Trailing whitespace at the truncation boundary must be stripped."""
        core = "y" * (DESCRIPTION_SHORT_MAX_CHARS - 5)
        trailing = "   " + "z" * 200  # spaces fall within the 120-char window
        long_summary = core + trailing
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, summary=long_summary, summary_short="")
        assert not da.summary_short.endswith(" ")
        assert da.summary_short == long_summary[:DESCRIPTION_SHORT_MAX_CHARS].rstrip()

    def test_summary_short_not_overwritten_when_provided(self):
        """An explicitly provided summary_short must not be overwritten."""
        da = DocumentAnalysis(
            document_integrity=VALID_INTEGRITY,
            summary_short="Short desc.",
            summary="Much longer summary text that should not replace the short one.",
        )
        assert da.summary_short == "Short desc."

    def test_summary_short_stays_empty_when_no_summary(self):
        """When both summary and summary_short are empty, summary_short remains empty."""
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, summary_short="", summary="")
        assert da.summary_short == ""

    # --- German / lenient coercion tests ---

    def test_german_document_type_rechnung(self):
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, document_type="Rechnung")
        assert da.document_type == "Invoice"

    def test_german_document_type_vertrag(self):
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, document_type="vertrag")
        assert da.document_type == "Contract"

    def test_german_document_type_kontoauszug(self):
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, document_type="Kontoauszug")
        assert da.document_type == "BankStatement"

    def test_document_type_lowercase_canonical_accepted(self):
        """Lowercase versions of canonical type names should be accepted."""
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, document_type="invoice")
        assert da.document_type == "Invoice"

    def test_german_null_date_becomes_none(self):
        """German strings expressing 'unknown' should become None."""
        for val in (
            "unbekannt", "Unbekannt", "nicht angegeben", "Nicht angegeben",
            "n/a", "N/A", "na", "-", "—", "null", "none",
            "keine angabe", "nicht bekannt", "unknown", "not available",
        ):
            da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, document_date=val)
            assert da.document_date is None, f"expected None for {val!r}"

    def test_confidence_percentage_normalized(self):
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, confidence=85)
        assert da.confidence == pytest.approx(0.85)

    def test_confidence_fraction_unchanged(self):
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, confidence=0.85)
        assert da.confidence == pytest.approx(0.85)

    def test_confidence_over_100_clamped(self):
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, confidence=150)
        assert da.confidence == pytest.approx(1.0)

    def test_tags_as_string_split_to_list(self):
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, tags="invoice, insurance, car")
        assert da.tags == ["invoice", "insurance", "car"]

    def test_tags_non_list_becomes_empty(self):
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, tags=None)
        assert da.tags == []

    def test_key_points_as_newline_string(self):
        da = DocumentAnalysis(document_integrity=VALID_INTEGRITY, key_points="Point A\nPoint B\nPoint C")
        assert da.key_points == ["Point A", "Point B", "Point C"]

    def test_amounts_invalid_entries_dropped(self):
        """Non-dict entries in 'amounts' should be silently dropped."""
        data = {
            "amounts": [
                {"label": "total", "amount": "500,00"},
                "not a dict",
                None,
                {"label": "fee", "amount": "12,50"},
            ],
            "document_integrity": VALID_INTEGRITY,
        }
        da = DocumentAnalysis.model_validate(data)
        assert len(da.amounts) == 2
        assert da.amounts[0].label == "total"
        assert da.amounts[0].amount == pytest.approx(500.0)
        assert da.amounts[1].label == "fee"
        assert da.amounts[1].amount == pytest.approx(12.50)


    def test_document_integrity_complete_valid(self):
        integrity = DocumentIntegrity.model_validate(VALID_INTEGRITY)
        assert integrity.possible_multi_document_scan is False
        assert integrity.suspected_document_count == 1

    def test_missing_document_integrity_rejected(self):
        with pytest.raises(ValidationError):
            DocumentAnalysis.model_validate({"document_type": "Invoice"})

    def test_invalid_document_integrity_recommended_action_rejected(self):
        bad = {**VALID_INTEGRITY, "recommended_action": "split_now"}
        with pytest.raises(ValidationError):
            DocumentAnalysis.model_validate({"document_integrity": bad})

    def test_document_integrity_confidence_bounds(self):
        bad = {**VALID_INTEGRITY, "confidence": -0.1}
        with pytest.raises(ValidationError):
            DocumentAnalysis.model_validate({"document_integrity": bad})

    def test_document_integrity_confidence_percentage_normalized(self):
        da = DocumentAnalysis.model_validate({
            "document_integrity": {**VALID_INTEGRITY, "confidence": 91}
        })
        assert da.document_integrity.confidence == pytest.approx(0.91)

    def test_document_integrity_confidence_over_100_clamped(self):
        da = DocumentAnalysis.model_validate({
            "document_integrity": {**VALID_INTEGRITY, "confidence": 150}
        })
        assert da.document_integrity.confidence == pytest.approx(1.0)

    def test_json_roundtrip(self):
        da = DocumentAnalysis(
            document_integrity=VALID_INTEGRITY,
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
            document_integrity=VALID_INTEGRITY,
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


    def test_document_integrity_complete_valid(self):
        integrity = DocumentIntegrity.model_validate(VALID_INTEGRITY)
        assert integrity.possible_multi_document_scan is False
        assert integrity.suspected_document_count == 1

    def test_missing_document_integrity_rejected(self):
        with pytest.raises(ValidationError):
            DocumentAnalysis.model_validate({"document_type": "Invoice"})

    def test_invalid_document_integrity_recommended_action_rejected(self):
        bad = {**VALID_INTEGRITY, "recommended_action": "split_now"}
        with pytest.raises(ValidationError):
            DocumentAnalysis.model_validate({"document_integrity": bad})

    def test_document_integrity_confidence_bounds(self):
        bad = {**VALID_INTEGRITY, "confidence": -0.1}
        with pytest.raises(ValidationError):
            DocumentAnalysis.model_validate({"document_integrity": bad})

    def test_document_integrity_confidence_percentage_normalized(self):
        da = DocumentAnalysis.model_validate({
            "document_integrity": {**VALID_INTEGRITY, "confidence": 91}
        })
        assert da.document_integrity.confidence == pytest.approx(0.91)

    def test_document_integrity_confidence_over_100_clamped(self):
        da = DocumentAnalysis.model_validate({
            "document_integrity": {**VALID_INTEGRITY, "confidence": 150}
        })
        assert da.document_integrity.confidence == pytest.approx(1.0)

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
