"""LLM interaction: build prompt, call Ollama, parse and validate response."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from aktenfux.schema import DESCRIPTION_SHORT_MAX_CHARS, DocumentAnalysis

logger = logging.getLogger(__name__)

_LANGUAGE_NAMES = {
    "de": "German",
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "nl": "Dutch",
    "pt": "Portuguese",
}

_SUMMARIZE_SYSTEM_PROMPT = (
    "You are a document analyst. "
    "Read the OCR text of a document and write a detailed plain-text summary. "
    "Include: document type, date, sender/correspondent, key topics, important numbers "
    "(amounts, account numbers, contract numbers, invoice numbers, customer numbers), "
    "deadlines, required actions, and a concise list of key points. "
    "Do NOT use JSON. Write only plain text. "
    "IMPORTANT: The entire response MUST be in the requested language."
)

_ANALYZE_SYSTEM_PROMPT = (
    "You are a document analyst for private documents. "
    "You are given a pre-written summary of a document. "
    "Extract structured metadata from the summary and reply ONLY with valid JSON. "
    "Do not invent information not mentioned in the summary. "
    "If a value is not clearly present, use null, 'Other', or an empty list. "
    "Use only the allowed categories. "
    "Create safe filenames. "
    "All human-readable text values in the JSON must be in the requested language."
)

_REPAIR_SUFFIX = (
    "\n\nYour previous response was not valid JSON. "
    "Please reply ONLY with valid JSON, no markdown, no explanation."
)

# JSON schema template shown verbatim in every Pass 2 prompt.
# A concrete filled-in example is the most universally understood format for
# instruction-tuned LLMs. Using real JSON types (null, bool, number) avoids
# ambiguity – e.g. the LLM outputting the string "null" instead of JSON null.
_JSON_SCHEMA_TEMPLATE = """\
{
  "document_date": "2024-03-15",
  "correspondent": "Example Corp GmbH",
  "document_type": "Invoice",
  "topic": "Annual Software License",
  "category": "Invoices",
  "tags": ["invoice", "software", "annual"],
  "summary_short": "Invoice for annual software license renewal.",
  "summary": "Full multi-sentence description of what this document contains and its context.",
  "key_points": ["License fee: EUR 1200", "Valid until 2025-03-14"],
  "action_required": true,
  "action_summary": "Pay invoice by due date",
  "deadline": "2024-04-15",
  "amounts": [
    {"label": "Net", "amount": 1008.40, "currency": "EUR"},
    {"label": "VAT", "amount": 191.60, "currency": "EUR"},
    {"label": "Total", "amount": 1200.00, "currency": "EUR"}
  ],
  "entities": {
    "people": ["John Doe"],
    "organizations": ["Example Corp GmbH"],
    "addresses": ["Musterstrasse 1, 10115 Berlin"],
    "contract_numbers": ["V-2024-001"],
    "invoice_numbers": ["RE-2024-12345"],
    "customer_numbers": ["K-98765"]
  },
  "suggested_folder": "Invoices/Example-Corp",
  "suggested_filename": "2024-03-15_Example-Corp_Invoice_Software-License.pdf",
  "confidence": 0.90
}"""

# Field-level constraints shown in the prompt as plain text (separate from the
# JSON example so the LLM does not confuse them with required literal values).
_JSON_FIELD_CONSTRAINTS = (
    "Field constraints:\n"
    "- document_date, deadline: use YYYY-MM-DD format, or null if not present in the document.\n"
    "- correspondent: name of the sender or issuer, or null if unknown.\n"
    "- document_type: must be exactly one of: "
    "Invoice, Contract, Notice, Policy, BankStatement, Letter, Receipt, Manual, Other.\n"
    "- action_summary: set to null when action_required is false.\n"
    "- confidence: a float between 0.0 (uncertain) and 1.0 (certain).\n"
    "- summary_short: a single sentence, 120 characters maximum.\n"
    "- key_points: up to 5 short bullet points.\n"
    "- amounts: use decimal point as separator (e.g. 1500.00, not 1.500,00).\n"
    "Use null for any field that is not present in the document."
)


def _build_summarize_prompt(ocr_text: str, language: str) -> str:
    language_label = _language_label(language)
    return (
        f"Target language: {language_label}\n"
        f"IMPORTANT: Respond ONLY in {language_label}. Do not use any other language.\n\n"
        "Please write a detailed summary of the following OCR text:\n\n"
        f"{ocr_text}"
    )


def _build_analysis_prompt(
    summary: str,
    language: str,
    allowed_categories: list[str],
) -> str:
    language_label = _language_label(language)
    categories_str = ", ".join(f'"{c}"' for c in allowed_categories)
    return (
        f"Target language: {language_label}\n"
        f"IMPORTANT: All human-readable values in the JSON MUST be in {language_label}. "
        "Keep JSON field names unchanged.\n"
        f"Allowed categories: [{categories_str}]\n\n"
        "Return ONLY a JSON object with the same structure as the example below.\n"
        "Extract the actual values from the document summary; do not copy the example values.\n\n"
        f"Example structure:\n{_JSON_SCHEMA_TEMPLATE}\n\n"
        f"{_JSON_FIELD_CONSTRAINTS}\n\n"
        "Document summary to analyse:\n\n"
        f"{summary}"
    )


def _language_label(language: str) -> str:
    normalized = language.strip().lower()
    name = _LANGUAGE_NAMES.get(normalized)
    if name:
        return f"{name} ({normalized})"
    return language.strip()


def _call_ollama(
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float = 120.0,
    *,
    use_json_format: bool = True,
) -> str:
    """Call Ollama's /api/chat endpoint. Returns the raw response text."""
    input_chars = len(system_prompt) + len(user_prompt)
    input_tokens_estimate = input_chars // 4
    logger.debug(
        "Calling Ollama: url=%s model=%s timeout=%.0fs "
        "input_chars=%d input_tokens_estimate=%d",
        base_url,
        model,
        timeout,
        input_chars,
        input_tokens_estimate,
    )
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
    }
    if use_json_format:
        payload["format"] = "json"
    with httpx.Client(timeout=timeout) as client:
        response = client.post(f"{base_url}/api/chat", json=payload)
        response.raise_for_status()
        data = response.json()

    # The chat endpoint wraps the response in message.content
    content = data.get("message", {}).get("content", "")
    output_chars = len(content)

    # Ollama reports durations in nanoseconds; convert to milliseconds for readability.
    _ns_to_ms = 1_000_000
    total_duration_ms = data.get("total_duration", 0) // _ns_to_ms
    prompt_eval_duration_ms = data.get("prompt_eval_duration", 0) // _ns_to_ms
    eval_duration_ms = data.get("eval_duration", 0) // _ns_to_ms
    eval_count = data.get("eval_count", 0)

    logger.debug(
        "Ollama response: model=%s output_chars=%d eval_count=%d "
        "total_duration=%dms prompt_eval_duration=%dms eval_duration=%dms",
        model,
        output_chars,
        eval_count,
        total_duration_ms,
        prompt_eval_duration_ms,
        eval_duration_ms,
    )
    logger.debug("Ollama raw output:\n%s", content)
    return content


def _extract_json(text: str) -> str:
    """Strip markdown fences and return the JSON content."""
    # Remove ```json … ``` or ``` … ``` blocks
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)
    return text.strip()


def _parse_analysis(raw: str) -> DocumentAnalysis:
    cleaned = _extract_json(raw)
    data = json.loads(cleaned)
    return DocumentAnalysis.model_validate(data)


def _summarize_with_llm(
    ocr_text: str,
    *,
    base_url: str,
    model: str,
    language: str,
    timeout: float,
) -> str:
    """Call the LLM to produce a plain-text summary of *ocr_text* (pass 1)."""
    user_prompt = _build_summarize_prompt(ocr_text, language)
    logger.debug("Pass 1 – summarizing document: text_chars=%d", len(ocr_text))
    summary = _call_ollama(
        base_url,
        model,
        _SUMMARIZE_SYSTEM_PROMPT,
        user_prompt,
        timeout=timeout,
        use_json_format=False,
    )
    logger.debug("Pass 1 – summary generated: summary_chars=%d", len(summary))
    logger.debug("Pass 1 – full summary:\n%s", summary)
    return summary


def _apply_description_fallback(analysis: DocumentAnalysis, plain_summary: str) -> None:
    """Ensure *analysis.summary* and *analysis.summary_short* are never empty.

    The schema validator already fills *summary_short* from *summary* when present.
    This function provides a last-resort fallback using the pass-1 plain-text summary:

    * If *analysis.summary* is blank, the full pass-1 text is stored there so the
      complete description is preserved (not just the truncated short version).
    * If *analysis.summary_short* is still blank after the above, the first
      ``DESCRIPTION_SHORT_MAX_CHARS`` characters of the pass-1 text are used.

    A DEBUG log is emitted for each field that is filled this way.
    """
    stripped_summary = plain_summary.strip()
    if not stripped_summary:
        return

    if not analysis.summary.strip():
        analysis.summary = stripped_summary
        logger.debug(
            "summary was empty; filled from pass-1 plain-text summary (%d chars)",
            len(analysis.summary),
        )

    if not analysis.summary_short.strip():
        analysis.summary_short = stripped_summary[:DESCRIPTION_SHORT_MAX_CHARS].rstrip()
        logger.debug(
            "summary_short was empty; filled from pass-1 plain-text summary (%d chars)",
            len(analysis.summary_short),
        )


def analyze_document(
    ocr_text: str,
    *,
    base_url: str,
    model: str,
    language: str = "de",
    allowed_categories: list[str],
    timeout: float = 120.0,
) -> tuple[DocumentAnalysis, list[str]]:
    """Analyze *ocr_text* with the local LLM using a two-pass approach.

    Pass 1 – Summarize: the LLM produces a plain-text summary from the raw OCR
    text, allowing it to focus purely on reading comprehension.

    Pass 2 – Structure: the clean summary is fed back to the LLM which extracts
    the structured JSON metadata.  Using a concise, pre-digested input improves
    the accuracy of the JSON extraction step.

    Returns (DocumentAnalysis, warnings).
    On failure raises an exception after one retry of pass 2.
    """
    warnings: list[str] = []
    logger.debug(
        "Starting document analysis: model=%s url=%s timeout=%.0fs language=%s text_chars=%d",
        model,
        base_url,
        timeout,
        language,
        len(ocr_text),
    )

    # --- Pass 1: Generate plain-text summary ---
    summary = _summarize_with_llm(
        ocr_text,
        base_url=base_url,
        model=model,
        language=language,
        timeout=timeout,
    )

    # --- Pass 2: Extract structured JSON from summary ---
    logger.debug("Pass 2 – extracting structured metadata from summary")
    user_prompt = _build_analysis_prompt(summary, language, allowed_categories)

    raw = _call_ollama(base_url, model, _ANALYZE_SYSTEM_PROMPT, user_prompt, timeout=timeout)

    try:
        analysis = _parse_analysis(raw)
        _apply_description_fallback(analysis, summary)
        logger.debug(
            "LLM analysis succeeded: category=%s confidence=%.2f description=%r",
            analysis.category,
            analysis.confidence,
            analysis.summary_short,
        )
        return analysis, warnings
    except Exception as first_exc:  # noqa: BLE001
        logger.warning("LLM returned invalid JSON, retrying with repair prompt: %s", first_exc)
        logger.debug("Pass 2 failed – raw response was:\n%s", raw)
        warnings.append(f"First LLM response was invalid JSON: {first_exc}")

    # --- Retry with repair prompt ---
    logger.debug("Sending repair prompt to model=%s timeout=%.0fs", model, timeout)
    repair_prompt = user_prompt + _REPAIR_SUFFIX
    raw2 = _call_ollama(base_url, model, _ANALYZE_SYSTEM_PROMPT, repair_prompt, timeout=timeout)

    try:
        analysis = _parse_analysis(raw2)
        _apply_description_fallback(analysis, summary)
        logger.debug(
            "LLM analysis succeeded after repair: category=%s description=%r",
            analysis.category,
            analysis.summary_short,
        )
        return analysis, warnings
    except Exception as second_exc:  # noqa: BLE001
        logger.error("LLM returned invalid JSON after retry: %s", second_exc)
        logger.debug("Pass 2 repair failed – raw response was:\n%s", raw2)
        raise ValueError(
            f"LLM returned invalid JSON after repair attempt: {second_exc}"
        ) from second_exc
