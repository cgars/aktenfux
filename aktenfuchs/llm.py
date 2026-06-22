"""LLM interaction: build prompt, call Ollama, parse and validate response."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from aktenfuchs.schema import DESCRIPTION_SHORT_MAX_CHARS, DocumentAnalysis

logger = logging.getLogger(__name__)

_SUMMARIZE_SYSTEM_PROMPT = (
    "You are a document analyst. "
    "Read the OCR text of a document and write a detailed plain-text summary. "
    "Include: document type, date, sender/correspondent, key topics, important numbers "
    "(amounts, account numbers, contract numbers, invoice numbers, customer numbers), "
    "deadlines, required actions, and a concise list of key points. "
    "Do NOT use JSON. Write only plain text in the requested language."
)

_ANALYZE_SYSTEM_PROMPT = (
    "You are a document analyst for private documents. "
    "You are given a pre-written summary of a document. "
    "Extract structured metadata from the summary and reply ONLY with valid JSON. "
    "Do not invent information not mentioned in the summary. "
    "If a value is not clearly present, use null, 'Other', or an empty list. "
    "Use only the allowed categories. "
    "Create safe filenames."
)

_REPAIR_SUFFIX = (
    "\n\nYour previous response was not valid JSON. "
    "Please reply ONLY with valid JSON, no markdown, no explanation."
)


def _build_summarize_prompt(ocr_text: str, language: str) -> str:
    return (
        f"Language for the summary: {language}\n\n"
        "Please write a detailed summary of the following OCR text:\n\n"
        f"{ocr_text}"
    )


def _build_analysis_prompt(
    summary: str,
    language: str,
    allowed_categories: list[str],
) -> str:
    categories_str = ", ".join(f'"{c}"' for c in allowed_categories)
    return (
        f"Language for summaries: {language}\n"
        f"Allowed categories: [{categories_str}]\n\n"
        "Based on the following document summary, return ONLY a JSON object "
        "matching the required schema:\n\n"
        f"{summary}"
    )


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
    return summary


def _apply_description_fallback(analysis: DocumentAnalysis, plain_summary: str) -> None:
    """Ensure *analysis.summary_short* is never empty.

    The schema validator already fills it from *summary* when present.
    This function provides a last-resort fallback: the first
    ``DESCRIPTION_SHORT_MAX_CHARS`` characters of the pass-1 plain-text
    summary are used when both ``summary_short`` and ``summary`` are blank.
    A DEBUG log is emitted so the caller can see the fallback was triggered.
    """
    if not analysis.summary_short.strip() and plain_summary.strip():
        analysis.summary_short = plain_summary[:DESCRIPTION_SHORT_MAX_CHARS].rstrip()
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
        raise ValueError(
            f"LLM returned invalid JSON after repair attempt: {second_exc}"
        ) from second_exc
