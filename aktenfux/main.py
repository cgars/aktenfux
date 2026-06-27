"""Core document processing pipeline."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path

from aktenfux.config import AktenfuxConfig
from aktenfux.filenames import make_suggested_filename, make_suggested_folder, resolve_collision
from aktenfux.llm import analyze_document
from aktenfux.pdf_text import extract_text, has_usable_text, is_ignored_file, truncate_text
from aktenfux.schema import SidecarDocument
from aktenfux.storage import (
    assert_within_base,
    move_file_with_sidecar,
    read_sidecar,
    sha256_file,
    sidecar_path_for,
    write_markdown_summary,
    write_sidecar,
)

logger = logging.getLogger(__name__)

# Length of the hex document ID (128-bit UUID → 32 hex chars; we use 16).
_DOC_ID_LENGTH = 16


def _generate_id() -> str:
    return uuid.uuid4().hex[:_DOC_ID_LENGTH]


def process_inbox(config: AktenfuxConfig) -> None:
    """Process all PDFs currently in the inbox folder."""
    inbox = config.inbox_path
    logger.debug(
        "process_inbox config: base_dir=%s ollama_url=%s model=%s timeout=%.0fs "
        "max_chars=%d language=%s dry_run=%s",
        config.base_dir,
        config.ollama_url,
        config.ollama_model,
        config.ollama_timeout,
        config.max_chars_for_llm,
        config.language,
        config.dry_run,
    )
    if not inbox.exists():
        logger.warning("Inbox folder does not exist: %s", inbox)
        return

    pdfs = [p for p in sorted(inbox.glob("*.pdf")) if not is_ignored_file(p)]
    if not pdfs:
        logger.info("No PDFs found in inbox: %s", inbox)
        return

    logger.info("Found %d PDF(s) in inbox.", len(pdfs))

    try:
        import aktenfux.db as db  # noqa: PLC0415
        if config.use_sqlite_index:
            db.initialize_db(config.sqlite_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not initialize SQLite: %s", exc)

    for pdf in pdfs:
        try:
            _process_single(pdf, config)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to process %s: %s", pdf.name, exc)
            if not config.dry_run:
                _move_to_error(pdf, config, reason=str(exc))


def _process_single(pdf: Path, config: AktenfuxConfig) -> None:
    logger.info("Processing: %s", pdf.name)

    # --- Extract OCR text ---
    ocr_text = extract_text(pdf)
    if not has_usable_text(ocr_text):
        logger.warning("No usable OCR text in %s", pdf.name)
        if config.dry_run:
            logger.info("[DRY-RUN] Would move %s to _Error (no OCR text).", pdf.name)
        else:
            _move_to_error(pdf, config, reason="No usable OCR text found.")
        return

    truncated = truncate_text(ocr_text, config.max_chars_for_llm)
    if len(truncated) < len(ocr_text):
        logger.debug(
            "Text truncated for LLM: original=%d chars → %d chars (limit=%d)",
            len(ocr_text),
            len(truncated),
            config.max_chars_for_llm,
        )

    # --- Compute SHA-256 ---
    file_hash = sha256_file(pdf)
    logger.debug("SHA-256 for %s: %s", pdf.name, file_hash)

    warnings: list[str] = []

    # --- Duplicate check (SQLite) ---
    if config.use_sqlite_index:
        try:
            import aktenfux.db as db  # noqa: PLC0415
            existing = db.find_by_sha256(config.sqlite_path, file_hash)
            if existing:
                logger.warning(
                    "Duplicate detected for %s (sha256=%s, existing id=%s).",
                    pdf.name,
                    file_hash,
                    existing["id"],
                )
                warnings.append(
                    f"Duplicate detected (sha256={file_hash}, existing id={existing['id']})."
                )
                # Still proceed but add a warning to the sidecar.
        except Exception as exc:  # noqa: BLE001
            logger.warning("Duplicate check failed: %s", exc)

    # --- Call LLM ---
    analysis = None
    try:
        analysis, llm_warnings = analyze_document(
            truncated,
            base_url=config.ollama_url,
            model=config.ollama_model,
            language=config.language,
            allowed_categories=config.allowed_top_level_categories,
            timeout=config.ollama_timeout,
        )
        warnings.extend(llm_warnings)
    except Exception as exc:  # noqa: BLE001
        logger.error("LLM analysis failed for %s: %s", pdf.name, exc)
        if config.dry_run:
            logger.info("[DRY-RUN] Would move %s to _Error (LLM failure).", pdf.name)
            return
        _move_to_error(pdf, config, reason=f"LLM analysis failed: {exc}")
        return

    # --- Log description and warn if still empty after all fallbacks ---
    logger.debug(
        "Analysis for %s: description=%r category=%s confidence=%.0f%%",
        pdf.name,
        analysis.summary_short,
        analysis.category,
        analysis.confidence * 100,
    )
    if not analysis.summary_short.strip():
        logger.warning("summary_short is empty for %s; sidecar will have no description.", pdf.name)

    # --- Patch LLM suggestions if empty ---
    if not analysis.suggested_filename:
        analysis.suggested_filename = make_suggested_filename(
            analysis.document_date,
            analysis.correspondent,
            analysis.document_type,
            analysis.topic,
        )
    if not analysis.suggested_folder:
        analysis.suggested_folder = make_suggested_folder(
            analysis.category,
            analysis.correspondent,
            analysis.topic,
        )

    # --- Validate category ---
    if analysis.category not in config.allowed_top_level_categories:
        warnings.append(
            f"Category '{analysis.category}' not in allowed list; falling back to 'Other'."
        )
        analysis.category = "Other"

    # --- Build sidecar ---
    existing_sidecar = read_sidecar(pdf)
    doc_id = existing_sidecar.id if existing_sidecar is not None else _generate_id()
    original_path = (
        existing_sidecar.original_path if existing_sidecar is not None else str(pdf)
    )
    review_dest = resolve_collision(config.review_path / analysis.suggested_filename)

    sidecar = SidecarDocument.from_analysis(
        analysis,
        doc_id=doc_id,
        original_path=original_path,
        current_path=str(review_dest),
        sha256=file_hash,
        model=config.ollama_model,
        warnings=warnings,
    )

    if config.dry_run:
        logger.info(
            "[DRY-RUN] %s → _Review/%s (category=%s, confidence=%.0f%%)",
            pdf.name,
            review_dest.name,
            sidecar.category,
            sidecar.confidence * 100,
        )
        # Write the sidecar to the dry-run folder so the LLM analysis is preserved.
        dry_run_dest = config.dry_run_path / sidecar.suggested_filename
        dry_run_json = sidecar_path_for(dry_run_dest)
        sidecar.status = "dry_run"
        sidecar.current_path = str(dry_run_json)
        config.dry_run_path.mkdir(parents=True, exist_ok=True)
        written = write_sidecar(sidecar, dry_run_dest)
        logger.info("[DRY-RUN] Sidecar written to %s", written)
        return

    # --- Write sidecar next to the *current* (inbox) PDF before moving ---
    config.review_path.mkdir(parents=True, exist_ok=True)
    write_sidecar(sidecar, pdf)

    if config.write_markdown_summary:
        write_markdown_summary(sidecar, pdf)

    # --- Move PDF (and sidecar) to _Review ---
    move_file_with_sidecar(
        pdf,
        review_dest,
        base_dir=config.base_dir,
        dry_run=False,
        move_markdown=config.write_markdown_summary,
    )

    # Update sidecar current_path after move.
    sidecar.current_path = str(review_dest)
    write_sidecar(sidecar, review_dest)

    # --- Optional SQLite update ---
    if config.use_sqlite_index:
        try:
            import aktenfux.db as db  # noqa: PLC0415
            db.upsert_document(config.sqlite_path, sidecar)
        except Exception as exc:  # noqa: BLE001
            logger.warning("SQLite update failed: %s", exc)

    logger.info(
        "Processed %s → %s (id=%s)", pdf.name, review_dest.name, doc_id
    )


def _move_to_error(pdf: Path, config: AktenfuxConfig, reason: str = "") -> None:
    """Move a PDF to the _Error folder."""
    config.error_path.mkdir(parents=True, exist_ok=True)
    dest = resolve_collision(config.error_path / pdf.name)
    move_file_with_sidecar(
        pdf,
        dest,
        base_dir=config.base_dir,
        dry_run=False,
        move_markdown=False,
    )
    logger.info("Moved %s to _Error: %s", pdf.name, reason)


def approve_document(doc_id: str, config: AktenfuxConfig) -> None:
    """Move a reviewed document from _Review to Archive."""
    from aktenfux.review import find_document_by_id  # noqa: PLC0415

    result = find_document_by_id(config.review_path, doc_id)
    if result is None:
        logger.error("Document '%s' not found in _Review.", doc_id)
        raise FileNotFoundError(f"Document '{doc_id}' not found in _Review.")

    pdf_path, sidecar = result

    target_dir = config.archive_path / sidecar.suggested_folder
    target_pdf = resolve_collision(target_dir / sidecar.suggested_filename)
    assert_within_base(target_pdf, config.base_dir)

    if config.dry_run:
        logger.info("[DRY-RUN] Would approve %s → %s", pdf_path.name, target_pdf)
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    move_file_with_sidecar(
        pdf_path,
        target_pdf,
        base_dir=config.base_dir,
        dry_run=False,
        move_markdown=config.write_markdown_summary,
    )

    # Update sidecar after move.
    sidecar.status = "approved"
    sidecar.current_path = str(target_pdf)
    sidecar.approved_at = datetime.now().isoformat(timespec="seconds")
    write_sidecar(sidecar, target_pdf)

    if config.use_sqlite_index:
        try:
            import aktenfux.db as db  # noqa: PLC0415
            db.update_status(
                config.sqlite_path,
                doc_id,
                "approved",
                current_path=str(target_pdf),
                approved_at=sidecar.approved_at,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("SQLite update failed: %s", exc)

    logger.info("Approved %s → %s", pdf_path.name, target_pdf)


def reject_document(doc_id: str, config: AktenfuxConfig) -> None:
    """Move a reviewed document from _Review to _Error."""
    from aktenfux.review import find_document_by_id  # noqa: PLC0415

    result = find_document_by_id(config.review_path, doc_id)
    if result is None:
        logger.error("Document '%s' not found in _Review.", doc_id)
        raise FileNotFoundError(f"Document '{doc_id}' not found in _Review.")

    pdf_path, sidecar = result

    config.error_path.mkdir(parents=True, exist_ok=True)
    dest_pdf = resolve_collision(config.error_path / pdf_path.name)

    if config.dry_run:
        logger.info("[DRY-RUN] Would reject %s → _Error", pdf_path.name)
        return

    move_file_with_sidecar(
        pdf_path,
        dest_pdf,
        base_dir=config.base_dir,
        dry_run=False,
        move_markdown=False,
    )

    sidecar.status = "rejected"
    sidecar.current_path = str(dest_pdf)
    write_sidecar(sidecar, dest_pdf)

    if config.use_sqlite_index:
        try:
            import aktenfux.db as db  # noqa: PLC0415
            db.update_status(
                config.sqlite_path,
                doc_id,
                "rejected",
                current_path=str(dest_pdf),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("SQLite update failed: %s", exc)

    logger.info("Rejected %s → _Error", pdf_path.name)


def reprocess_document(doc_id: str, config: AktenfuxConfig) -> None:
    """Re-analyze a document from _Review."""
    from aktenfux.review import find_document_by_id  # noqa: PLC0415

    result = find_document_by_id(config.review_path, doc_id)
    if result is None:
        raise FileNotFoundError(f"Document '{doc_id}' not found in _Review.")

    pdf_path, _old_sidecar = result
    logger.info("Reprocessing %s …", pdf_path.name)
    _process_single(pdf_path, config)
