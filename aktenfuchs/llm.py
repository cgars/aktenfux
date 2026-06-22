"""LLM interaction: build prompt, call Ollama, parse and validate response."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from aktenfuchs.schema import DocumentAnalysis

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a document analyst for private documents. "
    "Analyze the OCR text conservatively. "
    "Reply only with valid JSON. "
    "Do not invent information. "
    "If a value is not clearly recognizable, use null, 'Other', or an empty list. "
    "Use only the allowed categories. "
    "Create safe filenames. "
    "Summarize the document briefly and extract key points, possible action items, "
    "deadlines, amounts, and relevant numbers."
)

_REPAIR_SUFFIX = (
    "\n\nYour previous response was not valid JSON. "
    "Please reply ONLY with valid JSON, no markdown, no explanation."
)


def _build_user_prompt(
    ocr_text: str,
    language: str,
    allowed_categories: list[str],
) -> str:
    categories_str = ", ".join(f'"{c}"' for c in allowed_categories)
    return (
        f"Language for summaries: {language}\n"
        f"Allowed categories: [{categories_str}]\n\n"
        "Analyze the following OCR text and return ONLY a JSON object "
        "matching the required schema:\n\n"
        f"{ocr_text}"
    )


def _call_ollama(
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float = 120.0,
) -> str:
    """Call Ollama's /api/chat endpoint. Returns the raw response text."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "format": "json",
    }
    with httpx.Client(timeout=timeout) as client:
        response = client.post(f"{base_url}/api/chat", json=payload)
        response.raise_for_status()
        data = response.json()

    # The chat endpoint wraps the response in message.content
    return data.get("message", {}).get("content", "")


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


def analyze_document(
    ocr_text: str,
    *,
    base_url: str,
    model: str,
    language: str = "de",
    allowed_categories: list[str],
) -> tuple[DocumentAnalysis, list[str]]:
    """Analyze *ocr_text* with the local LLM and return a validated result.

    Returns (DocumentAnalysis, warnings).
    On failure raises an exception after one retry.
    """
    warnings: list[str] = []
    user_prompt = _build_user_prompt(ocr_text, language, allowed_categories)

    raw = _call_ollama(base_url, model, _SYSTEM_PROMPT, user_prompt)

    try:
        analysis = _parse_analysis(raw)
        return analysis, warnings
    except Exception as first_exc:  # noqa: BLE001
        logger.warning("LLM returned invalid JSON, retrying with repair prompt: %s", first_exc)
        warnings.append(f"First LLM response was invalid JSON: {first_exc}")

    # --- Retry with repair prompt ---
    repair_prompt = user_prompt + _REPAIR_SUFFIX
    raw2 = _call_ollama(base_url, model, _SYSTEM_PROMPT, repair_prompt)

    try:
        analysis = _parse_analysis(raw2)
        return analysis, warnings
    except Exception as second_exc:  # noqa: BLE001
        logger.error("LLM returned invalid JSON after retry: %s", second_exc)
        raise ValueError(
            f"LLM returned invalid JSON after repair attempt: {second_exc}"
        ) from second_exc
