"""Tests for the two-pass LLM analysis in aktenfux/llm.py."""
from __future__ import annotations

import json
from unittest.mock import call, patch

import pytest

from aktenfux.llm import (
    _JSON_FIELD_CONSTRAINTS,
    _JSON_SCHEMA_TEMPLATE,
    _build_analysis_prompt,
    _build_summarize_prompt,
    _summarize_with_llm,
    analyze_document,
)
from aktenfux.schema import DESCRIPTION_SHORT_MAX_CHARS, DocumentAnalysis

_VALID_ANALYSIS_JSON = json.dumps(
    {
        "document_date": "2026-01-15",
        "correspondent": "Test GmbH",
        "document_type": "Invoice",
        "topic": "Software License",
        "category": "Invoices",
        "confidence": 0.85,
        "suggested_filename": "2026-01-15_Test-GmbH_Invoice_Software-License.pdf",
        "suggested_folder": "Invoices/Test-GmbH",
        "summary_short": "Invoice for software license.",
        "summary": "Test GmbH invoices a software license fee of EUR 500.",
        "key_points": ["Software license fee: EUR 500"],
        "action_required": False,
        "amounts": [{"label": "total", "amount": 500.0, "currency": "EUR"}],
        "entities": {"organizations": ["Test GmbH"]},
        "tags": ["invoice", "software"],
    }
)

_PLAIN_SUMMARY = (
    "This is an invoice from Test GmbH dated 2026-01-15 for a software license. "
    "Total amount: EUR 500. No action required."
)


class TestBuildSummarizePrompt:
    def test_includes_ocr_text(self):
        prompt = _build_summarize_prompt("some ocr content", "de")
        assert "some ocr content" in prompt

    def test_includes_language(self):
        prompt = _build_summarize_prompt("text", "en")
        assert "English (en)" in prompt

    def test_has_strong_language_instruction(self):
        prompt = _build_summarize_prompt("text", "en")
        assert "Respond ONLY in English (en)" in prompt

    def test_keeps_unknown_language_code(self):
        prompt = _build_summarize_prompt("text", "sv")
        assert "Target language: sv" in prompt


class TestBuildAnalysisPrompt:
    def test_includes_summary(self):
        prompt = _build_analysis_prompt("my summary", "de", ["Invoices", "Other"])
        assert "my summary" in prompt

    def test_includes_categories(self):
        prompt = _build_analysis_prompt("summary", "de", ["Taxes", "Banking"])
        assert "Taxes" in prompt
        assert "Banking" in prompt

    def test_includes_language(self):
        prompt = _build_analysis_prompt("summary", "en", ["Other"])
        assert "English (en)" in prompt

    def test_has_strong_language_instruction(self):
        prompt = _build_analysis_prompt("summary", "en", ["Other"])
        assert "MUST be in English (en)" in prompt

    def test_keeps_unknown_language_code(self):
        prompt = _build_analysis_prompt("summary", "sv", ["Other"])
        assert "Target language: sv" in prompt

    def test_includes_json_schema_template(self):
        """The analysis prompt must contain the full JSON schema template."""
        prompt = _build_analysis_prompt("any summary", "de", ["Other"])
        assert _JSON_SCHEMA_TEMPLATE in prompt

    def test_includes_field_constraints(self):
        """The analysis prompt must contain the field constraints text."""
        prompt = _build_analysis_prompt("any summary", "de", ["Other"])
        assert _JSON_FIELD_CONSTRAINTS in prompt

    def test_schema_template_is_valid_json(self):
        """The schema template must be parseable as valid JSON."""
        parsed = json.loads(_JSON_SCHEMA_TEMPLATE)
        assert isinstance(parsed, dict)

    def test_schema_template_contains_all_required_fields(self):
        """All DocumentAnalysis fields should be present in the schema template."""
        parsed = json.loads(_JSON_SCHEMA_TEMPLATE)
        required_fields = [
            "document_date", "correspondent", "document_type", "topic",
            "category", "tags", "summary_short", "summary", "key_points",
            "action_required", "action_summary", "deadline", "amounts",
            "entities", "suggested_folder", "suggested_filename", "confidence",
        ]
        for field in required_fields:
            assert field in parsed, f"Field '{field}' missing from schema template"

    def test_schema_template_nullable_fields_use_real_null(self):
        """Nullable fields in the template must use JSON null, not the string 'null'."""
        # The example uses real values for all fields (action_required=true with a
        # non-null action_summary). What matters is that no field contains the
        # literal string "null" as its value – nullability is conveyed in the
        # field constraints text, not by embedding "null" strings in the example.
        parsed = json.loads(_JSON_SCHEMA_TEMPLATE)
        # Verify no field contains the literal string "null" as its value
        def _has_string_null(obj: object) -> bool:
            if isinstance(obj, dict):
                return any(_has_string_null(v) for v in obj.values())
            if isinstance(obj, list):
                return any(_has_string_null(i) for i in obj)
            return obj == "null"
        assert not _has_string_null(parsed), "Template must not use the string 'null' as a value"

    def test_summary_appears_after_schema_template(self):
        """The document summary must appear after the schema template in the prompt."""
        summary = "unique-document-summary-content-xyz"
        prompt = _build_analysis_prompt(summary, "de", ["Other"])
        schema_pos = prompt.index(_JSON_SCHEMA_TEMPLATE)
        summary_pos = prompt.index(summary)
        assert summary_pos > schema_pos, "Summary should appear after the schema template"


class TestAnalyzeDocument:
    @patch("aktenfux.llm._call_ollama")
    def test_two_pass_calls_ollama_twice(self, mock_call):
        """analyze_document should make two LLM calls: summarize then analyze."""
        mock_call.side_effect = [_PLAIN_SUMMARY, _VALID_ANALYSIS_JSON]

        analysis, warnings = analyze_document(
            "Raw OCR text of invoice",
            base_url="http://localhost:11434",
            model="qwen3:8b",
            language="de",
            allowed_categories=["Invoices", "Other"],
        )

        assert mock_call.call_count == 2
        assert isinstance(analysis, DocumentAnalysis)
        assert warnings == []

    @patch("aktenfux.llm._call_ollama")
    def test_first_call_uses_plain_format(self, mock_call):
        """Pass 1 (summarize) must NOT request JSON format from Ollama."""
        mock_call.side_effect = [_PLAIN_SUMMARY, _VALID_ANALYSIS_JSON]

        analyze_document(
            "Raw OCR text",
            base_url="http://localhost:11434",
            model="qwen3:8b",
            language="de",
            allowed_categories=["Invoices", "Other"],
        )

        first_call_kwargs = mock_call.call_args_list[0].kwargs
        assert first_call_kwargs.get("use_json_format") is False

    @patch("aktenfux.llm._call_ollama")
    def test_second_call_uses_json_format(self, mock_call):
        """Pass 2 (analyze) must request JSON format from Ollama."""
        mock_call.side_effect = [_PLAIN_SUMMARY, _VALID_ANALYSIS_JSON]

        analyze_document(
            "Raw OCR text",
            base_url="http://localhost:11434",
            model="qwen3:8b",
            language="de",
            allowed_categories=["Invoices", "Other"],
        )

        second_call_kwargs = mock_call.call_args_list[1].kwargs
        # use_json_format defaults to True and is not passed explicitly
        assert second_call_kwargs.get("use_json_format", True) is True

    @patch("aktenfux.llm._call_ollama")
    def test_summary_passed_to_analysis_prompt(self, mock_call):
        """The plain-text summary from pass 1 must appear in the pass 2 user prompt."""
        mock_call.side_effect = [_PLAIN_SUMMARY, _VALID_ANALYSIS_JSON]

        analyze_document(
            "Raw OCR text",
            base_url="http://localhost:11434",
            model="qwen3:8b",
            language="de",
            allowed_categories=["Invoices", "Other"],
        )

        # user_prompt is the 4th positional arg of _call_ollama
        second_user_prompt = mock_call.call_args_list[1].args[3]
        assert _PLAIN_SUMMARY in second_user_prompt

    @patch("aktenfux.llm._call_ollama")
    def test_raw_ocr_not_in_analysis_prompt(self, mock_call):
        """Raw OCR text must NOT be passed directly to the JSON extraction step."""
        mock_call.side_effect = [_PLAIN_SUMMARY, _VALID_ANALYSIS_JSON]
        raw_ocr = "Distinctive raw OCR content xyz987"

        analyze_document(
            raw_ocr,
            base_url="http://localhost:11434",
            model="qwen3:8b",
            language="de",
            allowed_categories=["Invoices", "Other"],
        )

        # The second call's user prompt should not contain the raw OCR text
        second_user_prompt = mock_call.call_args_list[1].args[3]
        assert raw_ocr not in second_user_prompt

    @patch("aktenfux.llm._call_ollama")
    def test_analysis_result_fields(self, mock_call):
        """Parsed analysis should contain the fields from the JSON response."""
        mock_call.side_effect = [_PLAIN_SUMMARY, _VALID_ANALYSIS_JSON]

        analysis, _ = analyze_document(
            "Some OCR text",
            base_url="http://localhost:11434",
            model="qwen3:8b",
            language="de",
            allowed_categories=["Invoices", "Other"],
        )

        assert analysis.correspondent == "Test GmbH"
        assert analysis.category == "Invoices"
        assert analysis.confidence == pytest.approx(0.85)

    @patch("aktenfux.llm._call_ollama")
    def test_retry_on_invalid_json(self, mock_call):
        """If pass 2 returns invalid JSON, a repair call should be attempted."""
        mock_call.side_effect = [
            _PLAIN_SUMMARY,       # pass 1: summarize
            "not valid json",      # pass 2 first attempt
            _VALID_ANALYSIS_JSON,  # pass 2 repair attempt
        ]

        analysis, warnings = analyze_document(
            "Raw OCR text",
            base_url="http://localhost:11434",
            model="qwen3:8b",
            language="de",
            allowed_categories=["Invoices", "Other"],
        )

        assert mock_call.call_count == 3
        assert len(warnings) == 1
        assert "invalid JSON" in warnings[0]
        assert isinstance(analysis, DocumentAnalysis)

    @patch("aktenfux.llm._call_ollama")
    def test_raises_after_failed_retry(self, mock_call):
        """If both pass 2 attempts fail, ValueError should be raised."""
        mock_call.side_effect = [
            _PLAIN_SUMMARY,
            "invalid json 1",
            "invalid json 2",
        ]

        with pytest.raises(ValueError, match="repair attempt"):
            analyze_document(
                "Raw OCR text",
                base_url="http://localhost:11434",
                model="qwen3:8b",
                language="de",
                allowed_categories=["Invoices", "Other"],
            )

    @patch("aktenfux.llm._call_ollama")
    def test_timeout_passed_to_both_calls(self, mock_call):
        """The configured timeout must be forwarded to both LLM calls."""
        mock_call.side_effect = [_PLAIN_SUMMARY, _VALID_ANALYSIS_JSON]

        analyze_document(
            "Raw OCR text",
            base_url="http://localhost:11434",
            model="qwen3:8b",
            language="de",
            allowed_categories=["Other"],
            timeout=60.0,
        )

        for c in mock_call.call_args_list:
            assert c.kwargs.get("timeout") == 60.0 or (
                len(c.args) > 4 and c.args[4] == 60.0
            )


class TestSummarizeWithLlm:
    @patch("aktenfux.llm._call_ollama")
    def test_returns_summary_string(self, mock_call):
        mock_call.return_value = "A concise summary of the document."

        result = _summarize_with_llm(
            "OCR text here",
            base_url="http://localhost:11434",
            model="qwen3:8b",
            language="de",
            timeout=120.0,
        )

        assert result == "A concise summary of the document."

    @patch("aktenfux.llm._call_ollama")
    def test_calls_without_json_format(self, mock_call):
        mock_call.return_value = "Summary text."

        _summarize_with_llm(
            "OCR text",
            base_url="http://localhost:11434",
            model="qwen3:8b",
            language="de",
            timeout=120.0,
        )

        assert mock_call.call_args.kwargs.get("use_json_format") is False


class TestDescriptionFallback:
    """summary_short must never be empty after analyze_document returns."""

    def _make_json_without_summary_short(self) -> str:
        """Valid analysis JSON where summary_short and summary are both absent."""
        return json.dumps(
            {
                "document_type": "Invoice",
                "category": "Invoices",
                "confidence": 0.7,
                "suggested_filename": "invoice.pdf",
                "suggested_folder": "Invoices",
                "action_required": False,
            }
        )

    def _make_json_with_summary_only(self) -> str:
        """Valid analysis JSON where summary is present but summary_short is not."""
        return json.dumps(
            {
                "document_type": "Invoice",
                "category": "Invoices",
                "confidence": 0.7,
                "summary": "Full length summary of the invoice document.",
                "suggested_filename": "invoice.pdf",
                "suggested_folder": "Invoices",
                "action_required": False,
            }
        )

    @patch("aktenfux.llm._call_ollama")
    def test_summary_short_filled_from_pass1_when_both_empty(self, mock_call):
        """When JSON has no summary_short/summary, the pass-1 text is used."""
        plain_summary = "Plain text summary from pass 1."
        mock_call.side_effect = [plain_summary, self._make_json_without_summary_short()]

        analysis, _ = analyze_document(
            "Raw OCR",
            base_url="http://localhost:11434",
            model="qwen3:8b",
            language="de",
            allowed_categories=["Invoices", "Other"],
        )

        assert analysis.summary_short == plain_summary[:DESCRIPTION_SHORT_MAX_CHARS].rstrip()

    @patch("aktenfux.llm._call_ollama")
    def test_full_summary_filled_from_pass1_when_llm_omits_it(self, mock_call):
        """When JSON has no summary field, the full pass-1 plain text is stored in summary."""
        plain_summary = "Full detailed plain-text summary from pass 1 that is longer than 120 chars. " * 3
        assert len(plain_summary) > DESCRIPTION_SHORT_MAX_CHARS, "test fixture must exceed cap"
        mock_call.side_effect = [plain_summary, self._make_json_without_summary_short()]

        analysis, _ = analyze_document(
            "Raw OCR",
            base_url="http://localhost:11434",
            model="qwen3:8b",
            language="de",
            allowed_categories=["Invoices", "Other"],
        )

        assert analysis.summary == plain_summary.strip()
        # summary_short should be capped but summary must be the full text
        assert len(analysis.summary) > DESCRIPTION_SHORT_MAX_CHARS

    @patch("aktenfux.llm._call_ollama")
    def test_summary_not_overwritten_when_llm_provides_it(self, mock_call):
        """When the LLM provides summary in pass 2, it must not be replaced by pass-1 text."""
        mock_call.side_effect = [_PLAIN_SUMMARY, _VALID_ANALYSIS_JSON]

        analysis, _ = analyze_document(
            "Raw OCR",
            base_url="http://localhost:11434",
            model="qwen3:8b",
            language="de",
            allowed_categories=["Invoices", "Other"],
        )

        assert analysis.summary == "Test GmbH invoices a software license fee of EUR 500."

    @patch("aktenfux.llm._call_ollama")
    def test_summary_short_filled_from_summary_in_schema(self, mock_call):
        """When JSON has summary but no summary_short, schema fills summary_short."""
        mock_call.side_effect = ["plain summary", self._make_json_with_summary_only()]

        analysis, _ = analyze_document(
            "Raw OCR",
            base_url="http://localhost:11434",
            model="qwen3:8b",
            language="de",
            allowed_categories=["Invoices", "Other"],
        )

        assert analysis.summary_short != ""
        assert "invoice" in analysis.summary_short.lower()

    @patch("aktenfux.llm._call_ollama")
    def test_summary_short_not_overwritten_when_llm_provides_it(self, mock_call):
        """When the LLM provides summary_short, it must not be replaced."""
        mock_call.side_effect = [_PLAIN_SUMMARY, _VALID_ANALYSIS_JSON]

        analysis, _ = analyze_document(
            "Raw OCR",
            base_url="http://localhost:11434",
            model="qwen3:8b",
            language="de",
            allowed_categories=["Invoices", "Other"],
        )

        assert analysis.summary_short == "Invoice for software license."

    @patch("aktenfux.llm._call_ollama")
    def test_pass1_fallback_truncated_at_120_chars(self, mock_call):
        """Fallback from pass-1 summary is capped at DESCRIPTION_SHORT_MAX_CHARS."""
        long_summary = "A" * 300
        mock_call.side_effect = [long_summary, self._make_json_without_summary_short()]

        analysis, _ = analyze_document(
            "Raw OCR",
            base_url="http://localhost:11434",
            model="qwen3:8b",
            language="de",
            allowed_categories=["Invoices", "Other"],
        )

        assert analysis.summary_short == long_summary[:DESCRIPTION_SHORT_MAX_CHARS]

    @patch("aktenfux.llm._call_ollama")
    def test_pass1_fallback_rstrips_trailing_whitespace(self, mock_call):
        """Trailing whitespace at the truncation boundary is stripped."""
        # Put trailing spaces exactly at the cut point so rstrip matters
        core = "B" * (DESCRIPTION_SHORT_MAX_CHARS - 5)
        trailing = "   \t " + "C" * 200
        long_summary = core + trailing  # spaces fall inside the DESCRIPTION_SHORT_MAX_CHARS window
        mock_call.side_effect = [long_summary, self._make_json_without_summary_short()]

        analysis, _ = analyze_document(
            "Raw OCR",
            base_url="http://localhost:11434",
            model="qwen3:8b",
            language="de",
            allowed_categories=["Invoices", "Other"],
        )

        assert not analysis.summary_short.endswith(" ")
        assert not analysis.summary_short.endswith("\t")
        assert analysis.summary_short == long_summary.strip()[:DESCRIPTION_SHORT_MAX_CHARS].rstrip()
