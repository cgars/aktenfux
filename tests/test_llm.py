"""Tests for the two-pass LLM analysis in aktenfuchs/llm.py."""
from __future__ import annotations

import json
from unittest.mock import call, patch

import pytest

from aktenfuchs.llm import (
    _build_analysis_prompt,
    _build_summarize_prompt,
    _summarize_with_llm,
    analyze_document,
)
from aktenfuchs.schema import DocumentAnalysis

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
        assert "en" in prompt


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
        assert "en" in prompt


class TestAnalyzeDocument:
    @patch("aktenfuchs.llm._call_ollama")
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

    @patch("aktenfuchs.llm._call_ollama")
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

    @patch("aktenfuchs.llm._call_ollama")
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

    @patch("aktenfuchs.llm._call_ollama")
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

    @patch("aktenfuchs.llm._call_ollama")
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

    @patch("aktenfuchs.llm._call_ollama")
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

    @patch("aktenfuchs.llm._call_ollama")
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

    @patch("aktenfuchs.llm._call_ollama")
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

    @patch("aktenfuchs.llm._call_ollama")
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
    @patch("aktenfuchs.llm._call_ollama")
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

    @patch("aktenfuchs.llm._call_ollama")
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

    @patch("aktenfuchs.llm._call_ollama")
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

        from aktenfuchs.schema import DESCRIPTION_SHORT_MAX_CHARS
        assert analysis.summary_short == plain_summary[:DESCRIPTION_SHORT_MAX_CHARS].rstrip()

    @patch("aktenfuchs.llm._call_ollama")
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

    @patch("aktenfuchs.llm._call_ollama")
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

    @patch("aktenfuchs.llm._call_ollama")
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

        from aktenfuchs.schema import DESCRIPTION_SHORT_MAX_CHARS
        assert analysis.summary_short == long_summary[:DESCRIPTION_SHORT_MAX_CHARS]
