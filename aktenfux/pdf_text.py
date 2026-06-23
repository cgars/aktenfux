"""PDF text extraction using pypdf (with optional pdfplumber fallback)."""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Minimum number of non-whitespace characters to consider OCR text usable.
_MIN_USABLE_CHARS = 50

# Patterns that indicate temporary / sync-conflict files to ignore.
_IGNORE_SUFFIXES = {".tmp", ".partial", ".crdownload"}
_IGNORE_PREFIXES = ("~",)
_SYNC_CONFLICT_PATTERNS = (
    "conflict",  # OneDrive, Nextcloud
    "(conflicted copy",  # Dropbox
)


def is_ignored_file(path: Path) -> bool:
    """Return True for temporary or sync-conflict files that should be skipped."""
    name = path.name.lower()
    if path.suffix.lower() in _IGNORE_SUFFIXES:
        return True
    for prefix in _IGNORE_PREFIXES:
        if name.startswith(prefix):
            return True
    for pattern in _SYNC_CONFLICT_PATTERNS:
        if pattern in name:
            return True
    return False


def extract_text(pdf_path: Path) -> str:
    """Extract embedded OCR text from *pdf_path*.

    Tries pypdf first; falls back to pdfplumber if available.
    Returns the extracted text (may be empty).
    """
    try:
        return _extract_with_pypdf(pdf_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pypdf extraction failed for %s: %s", pdf_path, exc)

    logger.debug("Falling back to pdfplumber for %s", pdf_path.name)
    try:
        return _extract_with_pdfplumber(pdf_path)
    except ImportError:
        logger.debug("pdfplumber not installed, skipping fallback.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdfplumber extraction failed for %s: %s", pdf_path, exc)

    return ""


def _extract_with_pypdf(pdf_path: Path) -> str:
    import pypdf  # noqa: PLC0415

    text_parts: list[str] = []
    with pypdf.PdfReader(str(pdf_path)) as reader:
        page_count = len(reader.pages)
        for page in reader.pages:
            page_text = page.extract_text() or ""
            text_parts.append(page_text)
    text = "\n".join(text_parts)
    logger.debug(
        "pypdf extracted %d chars from %d page(s) in %s",
        len(text),
        page_count,
        pdf_path.name,
    )
    return text


def _extract_with_pdfplumber(pdf_path: Path) -> str:
    import pdfplumber  # noqa: PLC0415

    text_parts: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            text_parts.append(page_text)
    text = "\n".join(text_parts)
    logger.debug(
        "pdfplumber extracted %d chars from %d page(s) in %s",
        len(text),
        page_count,
        pdf_path.name,
    )
    return text


def has_usable_text(text: str, min_chars: int = _MIN_USABLE_CHARS) -> bool:
    """Return True if *text* contains at least *min_chars* non-whitespace characters."""
    stripped = "".join(text.split())
    return len(stripped) >= min_chars


def truncate_text(text: str, max_chars: int) -> str:
    """Truncate *text* to at most *max_chars* characters."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars]
