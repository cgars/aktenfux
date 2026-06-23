"""Safe filename and path generation for archived documents."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Maximum total length for a suggested filename (without extension).
_MAX_STEM_LENGTH = 120

# Characters that are safe in filenames across all platforms.
# Includes ASCII word characters (\w), spaces, hyphens, dots, and German umlauts.
# If you need to support additional Unicode characters, extend this pattern.
_SAFE_PATTERN = re.compile(r"[^\w\s\-_.äöüÄÖÜß]", re.UNICODE)
_WHITESPACE_PATTERN = re.compile(r"\s+")


def sanitize_component(value: str) -> str:
    """Remove unsafe characters and collapse whitespace in a filename component."""
    # Replace unsafe characters with nothing (keep letters, digits, spaces, hyphens,
    # underscores, dots, and German umlauts).
    sanitized = _SAFE_PATTERN.sub("", value)
    # Collapse repeated whitespace to a single space.
    sanitized = _WHITESPACE_PATTERN.sub(" ", sanitized).strip()
    return sanitized


def sanitize_path_component(value: str) -> str:
    """Sanitize a single path component (folder name)."""
    sanitized = sanitize_component(value)
    # Replace spaces with hyphens for folder names.
    return sanitized.replace(" ", "-")


def make_suggested_filename(
    document_date: str | None,
    correspondent: str | None,
    document_type: str,
    topic: str,
    extension: str = ".pdf",
) -> str:
    """Build a safe filename from document metadata.

    Format: YYYY-MM-DD_Sender_DocumentType_Topic.pdf
    """
    date_part = document_date if document_date else "no_date"
    sender_part = sanitize_component(correspondent) if correspondent else "Unknown"
    type_part = sanitize_component(document_type)
    topic_part = sanitize_component(topic)

    # Replace spaces with hyphens in each component.
    sender_part = sender_part.replace(" ", "-")
    type_part = type_part.replace(" ", "-")
    topic_part = topic_part.replace(" ", "-")

    # Remove empty components.
    parts = [p for p in [date_part, sender_part, type_part, topic_part] if p]
    stem = "_".join(parts)

    # Truncate if necessary.
    if len(stem) > _MAX_STEM_LENGTH:
        stem = stem[:_MAX_STEM_LENGTH]

    return stem + extension


def make_suggested_folder(category: str, correspondent: str | None, topic: str) -> str:
    """Build a relative archive folder path from document metadata.

    Example: Insurance/HUK-COBURG/Car
    """
    parts = [sanitize_path_component(category)]
    if correspondent:
        parts.append(sanitize_path_component(correspondent))
    if topic:
        # Use only the first two words of the topic to keep paths short.
        short_topic = " ".join(topic.split()[:2])
        parts.append(sanitize_path_component(short_topic))
    return "/".join(p for p in parts if p)


def resolve_collision(target: Path) -> Path:
    """If *target* exists, append a counter suffix (_001, _002, …) until unique."""
    if not target.exists():
        return target

    stem = target.stem
    suffix = target.suffix
    parent = target.parent

    for i in range(1, 1000):
        candidate = parent / f"{stem}_{i:03d}{suffix}"
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Could not find a free filename for {target}")


def normalize_unicode(text: str) -> str:
    """NFC-normalize a Unicode string (safe to call even without umlauts)."""
    return unicodedata.normalize("NFC", text)
