"""PDF metadata writing for Aktenfux.

This module provides :func:`write_pdf_metadata`, which embeds a short,
portable set of descriptive metadata into a PDF file using information
already stored in a :class:`~aktenfux.schema.SidecarDocument`.

The fields written correspond to the standard PDF document information
dictionary:

* **Title** – the document topic (``sidecar.topic``)
* **Author** – the correspondent / sender (``sidecar.correspondent``)
* **Subject** – short human-readable description (``sidecar.summary_short``,
  at most :data:`SUBJECT_MAX_CHARS` characters)
* **Keywords** – comma-separated tags (``sidecar.tags``)
* **Creator** – always ``"Aktenfux"``
* **Producer** – always ``"Aktenfux"``

The existing PDF content (pages, fonts, images, …) is **never modified**.
Only the document information dictionary is updated.

Configuration
-------------
Metadata writing is controlled by the ``write_pdf_metadata`` setting in
``config.yaml`` (boolean, default ``false``).  When disabled, calling this
module's functions has no effect.

Usage
-----
.. code-block:: python

    from aktenfux.pdf_metadata import write_pdf_metadata
    from aktenfux.schema import SidecarDocument

    sidecar: SidecarDocument = ...
    write_pdf_metadata(Path("document.pdf"), sidecar)
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from aktenfux.schema import SidecarDocument

logger = logging.getLogger(__name__)

# Maximum length for the Subject field to keep it short and portable.
SUBJECT_MAX_CHARS = 200

# Application name used for Creator and Producer fields.
_APP_NAME = "Aktenfux"


def build_pdf_metadata(sidecar: SidecarDocument) -> dict[str, str]:
    """Build a dict of PDF metadata key/value pairs from *sidecar*.

    Only fields with non-empty values are included.  Keys use the PDF
    information-dictionary convention (leading ``/``).

    Args:
        sidecar: The :class:`~aktenfux.schema.SidecarDocument` to read from.

    Returns:
        A mapping of PDF metadata keys to string values.
    """
    metadata: dict[str, str] = {}

    title = (sidecar.topic or "").strip()
    if title:
        metadata["/Title"] = title

    author = (sidecar.correspondent or "").strip()
    if author:
        metadata["/Author"] = author

    subject = (sidecar.summary_short or "").strip()
    if subject:
        metadata["/Subject"] = subject[:SUBJECT_MAX_CHARS]

    if sidecar.tags:
        keywords = ", ".join(tag.strip() for tag in sidecar.tags if tag.strip())
        if keywords:
            metadata["/Keywords"] = keywords

    metadata["/Creator"] = _APP_NAME
    metadata["/Producer"] = _APP_NAME

    return metadata


def write_pdf_metadata(pdf_path: Path, sidecar: SidecarDocument) -> None:
    """Write metadata derived from *sidecar* into the PDF at *pdf_path*.

    The PDF is updated **in-place**: a temporary file is written first and
    then atomically replaces the original.  The existing page content,
    fonts, images, and all other PDF structures are preserved unchanged.

    Missing or empty sidecar fields are silently skipped.

    Args:
        pdf_path: Path to the PDF file to update.
        sidecar: The sidecar document whose data is written as metadata.

    Raises:
        FileNotFoundError: If *pdf_path* does not exist.
        OSError: If the temporary file cannot be written or the replacement fails.
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    metadata = build_pdf_metadata(sidecar)

    logger.debug(
        "Writing PDF metadata to %s: %s",
        pdf_path.name,
        ", ".join(f"{k}={v!r}" for k, v in metadata.items()),
    )

    tmp_path = pdf_path.with_suffix(".tmp_meta")
    try:
        with PdfReader(str(pdf_path)) as reader:
            writer = PdfWriter()
            writer.append(reader)
            writer.add_metadata(metadata)

            with tmp_path.open("wb") as fh:
                writer.write(fh)

        tmp_path.replace(pdf_path)
    except Exception as exc:  # noqa: BLE001
        tmp_path.unlink(missing_ok=True)
        logger.error("Failed to write PDF metadata for %s: %s", pdf_path.name, exc)
        raise
