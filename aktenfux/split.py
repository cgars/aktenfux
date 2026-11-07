"""Recoverable document split planning and execution services."""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Iterable

from pypdf import PdfReader, PdfWriter

from aktenfux.config import AktenfuxConfig
from aktenfux.schema import (
    SidecarDocument,
    SplitBoundaryProviderResponse,
    SplitBoundarySuggestion,
    SplitOutputRecord,
    SplitPartPlan,
    SplitPartResult,
    SplitPlan,
    SplitProvenanceRecord,
    SplitResult,
)
from aktenfux.storage import assert_within_base, sha256_file, sidecar_path_for

logger = logging.getLogger(__name__)
_WORKBENCH_DOCUMENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_WORKBENCH_DISPLAY_NAME_MAX_CHARS = 200
_SUGGEST_MAX_PAGES = 64
_SUGGEST_MAX_CHARS_PER_PAGE = 1200
_SUGGEST_MAX_PAYLOAD_BYTES = 80_000
_SUGGEST_MAX_WARNINGS = 32
_SUGGEST_TIMEOUT_SECONDS = 120.0


class SplitError(RuntimeError):
    """Bounded, audit-safe application error for split workflows."""


@dataclass(frozen=True)
class SourceSnapshot:
    pdf_path: Path
    pdf_name: str
    sidecar_path: Path
    lifecycle_root: Path
    lifecycle_label: str
    sidecar: SidecarDocument
    source_bytes: bytes
    source_sha256: str
    page_count: int


@dataclass(frozen=True)
class SplitDocumentSummary:
    """Bounded read model shared by CLI and UI adapters."""

    document_id: str
    display_name: str
    page_count: int
    source_sha256: str


@dataclass(frozen=True)
class CompletedSplitSummary:
    """Read-only summary for retained completed split sources."""

    document_id: str
    retained_pdf: str
    source_sha256: str
    operation_id: str
    completed_at_utc: str
    outputs: tuple[str, ...]


@dataclass(frozen=True)
class CompletedSplitReconcileIssue:
    document_id: str
    source_pdf: str
    reason: str


@dataclass(frozen=True)
class CompletedSplitReconcileResult:
    dry_run: bool
    eligible: tuple[CompletedSplitSummary, ...]
    transitioned: tuple[CompletedSplitSummary, ...]
    issues: tuple[CompletedSplitReconcileIssue, ...]


@dataclass(frozen=True)
class PublishJournal:
    state: str
    outputs: tuple[str, ...]
    published: frozenset[str]


@dataclass(frozen=True)
class PageEvidence:
    page: int
    text: str
    truncated: bool
    missing: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(value: str, *, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise SplitError(f"Invalid split provenance timestamp in {field_name}.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise SplitError(f"Split provenance timestamp in {field_name} must be UTC.")
    return parsed


def _deterministic_operation_id(
    document_id: str, source_sha256: str, page_count: int, boundaries: list[int]
) -> str:
    payload = f"{document_id}|{source_sha256}|{page_count}|{','.join(str(v) for v in boundaries)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _normalize_boundaries(boundaries: Iterable[int], page_count: int) -> list[int]:
    normalized = sorted(boundaries)
    if not normalized:
        raise SplitError("At least one --before boundary is required.")
    if len(set(normalized)) != len(normalized):
        raise SplitError("Duplicate boundaries are not allowed.")
    for boundary in normalized:
        if boundary < 2 or boundary > page_count:
            raise SplitError(f"Boundary {boundary} is out of range (2..{page_count}).")
    return normalized


def _build_parts(source_stem: str, page_count: int, boundaries: list[int]) -> list[SplitPartPlan]:
    starts = [1, *boundaries]
    ends = [*(boundary - 1 for boundary in boundaries), page_count]
    parts: list[SplitPartPlan] = []
    for idx, (start, end) in enumerate(zip(starts, ends), start=1):
        if start > end:
            raise SplitError("Boundaries produce an empty split part.")
        parts.append(
            SplitPartPlan(
                part_number=idx,
                start_page=start,
                end_page=end,
                destination_filename=f"{source_stem}--part-{idx:02d}.pdf",
            )
        )
    return parts


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _validate_lifecycle_root(
    *,
    base_dir: Path,
    root: Path,
    configured_name: str,
    label: str,
    must_exist: bool,
) -> Path:
    """Validate one direct lifecycle root before any document access."""
    try:
        configured_path = Path(configured_name)
        resolved_base = base_dir.resolve()
        resolved_root = root.resolve()
    except (OSError, RuntimeError, TypeError) as exc:
        raise SplitError(f"Could not validate the configured {label} root.") from exc

    if (
        configured_path.is_absolute()
        or len(configured_path.parts) != 1
        or configured_path.name in {"", ".", ".."}
        or resolved_root == resolved_base
        or resolved_root.parent != resolved_base
    ):
        raise SplitError(f"Configured {label} root must be a direct child of base_dir.")
    try:
        if _is_link_or_junction(root):
            raise SplitError(f"Configured {label} root must not be a link or junction.")
        if root.exists() and not root.is_dir():
            raise SplitError(f"Configured {label} root is not a directory.")
        if must_exist and not root.is_dir():
            raise SplitError(f"Configured {label} root is missing.")
    except SplitError:
        raise
    except OSError as exc:
        raise SplitError(f"Could not validate the configured {label} root.") from exc
    return resolved_root


def _validate_split_roots(config: AktenfuxConfig, *, include_inbox: bool) -> None:
    resolved_split = _validate_lifecycle_root(
        base_dir=config.base_dir,
        root=config.split_path,
        configured_name=config.split_dir,
        label="split",
        must_exist=True,
    )
    if not include_inbox:
        return
    resolved_inbox = _validate_lifecycle_root(
        base_dir=config.base_dir,
        root=config.inbox_path,
        configured_name=config.inbox_dir,
        label="inbox",
        must_exist=False,
    )
    if resolved_split == resolved_inbox:
        raise SplitError("Configured split and inbox roots must be distinct.")
    try:
        if config.inbox_path.exists() and os.path.samefile(config.split_path, config.inbox_path):
            raise SplitError("Configured split and inbox roots must not alias each other.")
    except SplitError:
        raise
    except OSError as exc:
        raise SplitError("Could not verify split and inbox root identity.") from exc


def _validate_imported_root(config: AktenfuxConfig) -> Path:
    return _validate_lifecycle_root(
        base_dir=config.base_dir,
        root=config.imported_path,
        configured_name=config.imported_dir,
        label="imported",
        must_exist=False,
    )


def _validate_completed_roots(config: AktenfuxConfig, *, include_inbox: bool) -> Path:
    _validate_split_roots(config, include_inbox=include_inbox)
    imported_root = _validate_imported_root(config)
    resolved_split = config.split_path.resolve()
    if imported_root == resolved_split:
        raise SplitError("Configured imported and split roots must be distinct.")
    try:
        if config.imported_path.exists() and os.path.samefile(config.imported_path, config.split_path):
            raise SplitError("Configured imported and split roots must not alias each other.")
    except SplitError:
        raise
    except OSError as exc:
        raise SplitError("Could not verify imported and split root identity.") from exc
    if include_inbox:
        resolved_inbox = config.inbox_path.resolve()
        if imported_root == resolved_inbox:
            raise SplitError("Configured imported and inbox roots must be distinct.")
        try:
            if config.imported_path.exists() and config.inbox_path.exists():
                if os.path.samefile(config.imported_path, config.inbox_path):
                    raise SplitError("Configured imported and inbox roots must not alias each other.")
        except SplitError:
            raise
        except OSError as exc:
            raise SplitError("Could not verify imported and inbox root identity.") from exc
    return imported_root


def _validate_direct_regular_file(path: Path, *, root: Path, suffix: str) -> None:
    try:
        assert_within_base(path, root)
    except ValueError as exc:
        raise SplitError(f"{path.name} is outside the expected lifecycle root.") from exc
    if path.parent.resolve() != root.resolve():
        raise SplitError(f"{path.name} must be located directly in {root.name}.")
    if path.suffix.lower() != suffix:
        raise SplitError(f"{path.name} has invalid extension; expected {suffix}.")
    if path.is_symlink():
        raise SplitError(f"{path.name} must not be a symbolic link.")
    if not path.is_file():
        raise SplitError(f"{path.name} must be a regular file.")


def _validate_direct_destination_path(path: Path, *, root: Path) -> None:
    try:
        assert_within_base(path, root)
    except ValueError as exc:
        raise SplitError(f"{path.name} is outside the expected destination root.") from exc
    if path.parent.resolve() != root.resolve():
        raise SplitError(f"{path.name} must be written directly to {root.name}.")
    if path.suffix.lower() != ".pdf":
        raise SplitError(f"{path.name} has invalid extension; expected .pdf.")
    if path.exists():
        if path.is_symlink():
            raise SplitError(f"{path.name} must not be a symbolic link.")
        if not path.is_file():
            raise SplitError(f"{path.name} must be a regular file.")


def _read_sidecar_validated(sidecar_path: Path) -> SidecarDocument:
    try:
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        return SidecarDocument.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        raise SplitError(f"Sidecar for {sidecar_path.stem}.pdf is invalid.") from exc


def _find_source_by_document_id(
    root: Path,
    *,
    document_id: str,
    root_label: str,
) -> tuple[Path, Path, SidecarDocument] | None:
    if not root.exists():
        return None

    matches: list[tuple[Path, Path, SidecarDocument]] = []
    for pdf_path in sorted(root.glob("*.pdf")):
        try:
            _validate_direct_regular_file(pdf_path, root=root, suffix=".pdf")
            sidecar_path = sidecar_path_for(pdf_path)
            _validate_direct_regular_file(sidecar_path, root=root, suffix=".json")
            sidecar = _read_sidecar_validated(sidecar_path)
        except SplitError as exc:
            logger.warning("Skipping invalid %s candidate '%s': %s", root_label, pdf_path.name, exc)
            continue
        if sidecar.id == document_id:
            matches.append((pdf_path, sidecar_path, sidecar))

    if len(matches) > 1:
        raise SplitError(f"Duplicate document id found in {root_label}.")
    return matches[0] if matches else None


def _completed_operation_for_document(
    sidecar: SidecarDocument, *, operation_id: str | None = None
) -> SplitProvenanceRecord | None:
    completed = [
        operation
        for operation in sidecar.split_operations
        if operation.state == "completed"
        and (operation_id is None or operation.operation_id == operation_id)
    ]
    if not completed:
        return None
    if operation_id is None and len(completed) > 1:
        raise SplitError("Document has multiple completed split operations and needs reconciliation.")
    return completed[-1]


def _completed_suffix(operation_id: str) -> str:
    return f"--completed-{operation_id[:12]}"


def _completed_destination_paths(
    *,
    imported_root: Path,
    source_pdf_name: str,
    operation_id: str,
) -> tuple[Path, Path]:
    source = Path(source_pdf_name)
    destination_pdf = imported_root / f"{source.stem}{_completed_suffix(operation_id)}.pdf"
    return destination_pdf, destination_pdf.with_suffix(".json")


def _plan_source_filename_matches_snapshot(plan_source_filename: str, snapshot_name: str, operation_id: str) -> bool:
    if plan_source_filename == snapshot_name:
        return True
    suffix = f"{_completed_suffix(operation_id)}.pdf"
    if not snapshot_name.endswith(suffix):
        return False
    derived = f"{snapshot_name[: -len(suffix)]}.pdf"
    return plan_source_filename == derived


def _read_validated_snapshot(
    pdf_path: Path,
    sidecar_path: Path,
    sidecar: SidecarDocument,
    *,
    lifecycle_root: Path,
    lifecycle_label: str,
) -> SourceSnapshot:
    try:
        with pdf_path.open("rb") as fh:
            source_bytes = fh.read()
    except OSError as exc:
        raise SplitError(f"Could not read source PDF '{pdf_path.name}'.") from exc

    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if source_sha256 != sidecar.sha256:
        raise SplitError("Source PDF hash does not match its authoritative sidecar.")
    try:
        page_count = len(PdfReader(BytesIO(source_bytes)).pages)
    except Exception as exc:  # noqa: BLE001
        raise SplitError(f"Failed to parse source PDF '{pdf_path.name}'.") from exc

    return SourceSnapshot(
        pdf_path=pdf_path,
        pdf_name=pdf_path.name,
        sidecar_path=sidecar_path,
        lifecycle_root=lifecycle_root,
        lifecycle_label=lifecycle_label,
        sidecar=sidecar,
        source_bytes=source_bytes,
        source_sha256=source_sha256,
        page_count=page_count,
    )


def _read_snapshot(
    config: AktenfuxConfig,
    document_id: str,
    *,
    include_completed: bool,
    operation_id: str | None = None,
) -> SourceSnapshot:
    active_match = _find_source_by_document_id(
        config.split_path,
        document_id=document_id,
        root_label="_Split",
    )
    if active_match is not None:
        pdf_path, sidecar_path, sidecar = active_match
        completed = _completed_operation_for_document(sidecar, operation_id=operation_id)
        if completed is not None and not include_completed:
            raise SplitError(
                f"Document '{document_id}' is already completed; inspect retained sources in _Imported."
            )
        if completed is not None and operation_id is not None and completed.operation_id != operation_id:
            raise SplitError("Only an exact retry of the completed split operation is allowed.")
        return _read_validated_snapshot(
            pdf_path,
            sidecar_path,
            sidecar,
            lifecycle_root=config.split_path,
            lifecycle_label="split",
        )

    imported_match = _find_source_by_document_id(
        config.imported_path,
        document_id=document_id,
        root_label="_Imported",
    )
    if imported_match is None:
        raise SplitError(f"Document '{document_id}' not found in _Split.")

    imported_pdf, imported_sidecar_path, imported_sidecar = imported_match
    completed = _completed_operation_for_document(imported_sidecar, operation_id=operation_id)
    if completed is None:
        raise SplitError(
            f"Document '{document_id}' is retained in _Imported but has no completed split provenance."
        )
    if not include_completed:
        raise SplitError(
            f"Document '{document_id}' is already completed; inspect retained sources in _Imported."
        )
    if operation_id is not None and completed.operation_id != operation_id:
        raise SplitError("Only an exact retry of the completed split operation is allowed.")
    return _read_validated_snapshot(
        imported_pdf,
        imported_sidecar_path,
        imported_sidecar,
        lifecycle_root=config.imported_path,
        lifecycle_label="imported",
    )


def _workbench_display_name(value: str, fallback: str) -> str:
    cleaned = "".join(character if character.isprintable() else "�" for character in value)
    cleaned = cleaned.strip()
    if not cleaned:
        cleaned = fallback
    return cleaned[:_WORKBENCH_DISPLAY_NAME_MAX_CHARS]


def list_split_documents(config: AktenfuxConfig) -> list[SplitDocumentSummary]:
    """Return stable document IDs and safe display metadata for eligible _Split PDFs."""
    _validate_split_roots(config, include_inbox=False)
    entries: list[SplitDocumentSummary] = []
    seen_ids: set[str] = set()
    for pdf_path in sorted(config.split_path.glob("*.pdf")):
        try:
            _validate_direct_regular_file(pdf_path, root=config.split_path, suffix=".pdf")
            sidecar_path = sidecar_path_for(pdf_path)
            _validate_direct_regular_file(
                sidecar_path, root=config.split_path, suffix=".json"
            )
            sidecar = _read_sidecar_validated(sidecar_path)
            if not _WORKBENCH_DOCUMENT_ID_PATTERN.fullmatch(sidecar.id):
                raise SplitError("Candidate has an invalid document id.")
            if _completed_operation_for_document(sidecar) is not None:
                continue
            snapshot = _read_validated_snapshot(
                pdf_path,
                sidecar_path,
                sidecar,
                lifecycle_root=config.split_path,
                lifecycle_label="split",
            )
        except (SplitError, ValueError, OSError):
            logger.warning("Skipping invalid _Split workbench candidate.")
            continue
        if sidecar.id in seen_ids:
            raise SplitError("Duplicate document id found in _Split.")
        seen_ids.add(sidecar.id)
        entries.append(
            SplitDocumentSummary(
                document_id=sidecar.id,
                display_name=_workbench_display_name(
                    sidecar.suggested_filename or pdf_path.name,
                    pdf_path.name,
                ),
                page_count=snapshot.page_count,
                source_sha256=snapshot.source_sha256,
            )
        )
    return entries


def get_split_document_snapshot(config: AktenfuxConfig, document_id: str) -> SourceSnapshot:
    """Return the validated source snapshot for a _Split document."""
    _validate_split_roots(config, include_inbox=False)
    return _read_snapshot(config, document_id, include_completed=False)


def _bounded_page_evidence(snapshot: SourceSnapshot) -> tuple[list[PageEvidence], list[str]]:
    warnings: list[str] = []
    evidence: list[PageEvidence] = []
    try:
        reader = PdfReader(BytesIO(snapshot.source_bytes))
    except Exception as exc:  # noqa: BLE001
        raise SplitError(f"Failed to parse source PDF '{snapshot.pdf_name}'.") from exc

    if snapshot.page_count > _SUGGEST_MAX_PAGES:
        warnings.append(
            f"Only the first {_SUGGEST_MAX_PAGES} pages were analysed for split suggestion."
        )

    for index, page in enumerate(reader.pages[:_SUGGEST_MAX_PAGES], start=1):
        try:
            raw_text = (page.extract_text() or "").strip()
        except Exception as exc:  # noqa: BLE001
            raise SplitError("Could not extract page-level evidence for split suggestion.") from exc
        if not raw_text:
            warnings.append(f"Page {index} has no extractable text.")
            evidence.append(PageEvidence(page=index, text="", truncated=False, missing=True))
            continue
        truncated = len(raw_text) > _SUGGEST_MAX_CHARS_PER_PAGE
        if truncated:
            warnings.append(f"Page {index} text was truncated for split suggestion.")
        evidence.append(
            PageEvidence(
                page=index,
                text=raw_text[:_SUGGEST_MAX_CHARS_PER_PAGE],
                truncated=truncated,
                missing=False,
            )
        )

    return evidence, warnings


def _compact_json_payload_size(payload: dict[str, Any]) -> int:
    return len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _truncate_utf8_bytes(text: str, max_bytes: int) -> str:
    if max_bytes <= 0 or not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _build_split_suggestion_request_payload(
    snapshot: SourceSnapshot, page_evidence: list[PageEvidence]
) -> dict[str, Any]:
    return {
        "document_id": snapshot.sidecar.id,
        "source_sha256": snapshot.source_sha256,
        "page_count": snapshot.page_count,
        "pages": [
            {
                "page": item.page,
                "text": item.text,
                "truncated": item.truncated,
                "missing": item.missing,
            }
            for item in page_evidence
        ],
        "instructions": {
            "return_json_only": True,
            "max_candidates": 64,
            "max_evidence_items_per_candidate": 8,
            "boundary_semantics": (
                "before_page is the first physical PDF page of the new logical document; "
                "if the previous document ends on page 3 and the new document starts on page 4, "
                "return before_page 4, never 3"
            ),
        },
    }


def _fit_page_evidence_to_request_budget(
    snapshot: SourceSnapshot, page_evidence: list[PageEvidence]
) -> tuple[list[PageEvidence], list[str]]:
    request_payload = _build_split_suggestion_request_payload(snapshot, page_evidence)
    if _compact_json_payload_size(request_payload) <= _SUGGEST_MAX_PAYLOAD_BYTES:
        return page_evidence, []

    def apply_utf8_page_text_cap(cap_bytes: int) -> tuple[list[PageEvidence], list[int]]:
        adjusted: list[PageEvidence] = []
        newly_truncated_pages: list[int] = []
        for item in page_evidence:
            if not item.text:
                adjusted.append(item)
                continue
            bounded_text = _truncate_utf8_bytes(item.text, cap_bytes)
            changed = bounded_text != item.text
            adjusted.append(
                PageEvidence(
                    page=item.page,
                    text=bounded_text,
                    truncated=item.truncated or changed,
                    missing=item.missing,
                )
            )
            if changed and not item.truncated:
                newly_truncated_pages.append(item.page)
        return adjusted, newly_truncated_pages

    min_evidence, _ = apply_utf8_page_text_cap(0)
    min_payload = _build_split_suggestion_request_payload(snapshot, min_evidence)
    if _compact_json_payload_size(min_payload) > _SUGGEST_MAX_PAYLOAD_BYTES:
        raise SplitError("Split suggestion request exceeded the allowed size limit.")

    max_text_bytes = max((len(item.text.encode("utf-8")) for item in page_evidence), default=0)
    low = 0
    high = max_text_bytes
    best_evidence = min_evidence
    best_newly_truncated_pages: list[int] = []
    while low <= high:
        mid = (low + high) // 2
        candidate_evidence, candidate_newly_truncated = apply_utf8_page_text_cap(mid)
        candidate_payload = _build_split_suggestion_request_payload(snapshot, candidate_evidence)
        if _compact_json_payload_size(candidate_payload) <= _SUGGEST_MAX_PAYLOAD_BYTES:
            best_evidence = candidate_evidence
            best_newly_truncated_pages = candidate_newly_truncated
            low = mid + 1
        else:
            high = mid - 1

    budget_warnings = [
        f"Page {page} text was truncated for split suggestion."
        for page in best_newly_truncated_pages
    ]
    return best_evidence, budget_warnings


def _bound_combined_suggestion_warnings(
    extraction_warnings: list[str], provider_warnings: list[str]
) -> list[str]:
    combined = [*extraction_warnings, *provider_warnings]
    if len(combined) <= _SUGGEST_MAX_WARNINGS:
        return combined

    retained = combined[: _SUGGEST_MAX_WARNINGS - 1]
    omitted_count = len(combined) - len(retained)
    retained.append(
        f"{omitted_count} additional split-suggestion warning(s) were omitted after reaching the "
        f"{_SUGGEST_MAX_WARNINGS}-warning limit."
    )
    return retained


def _default_split_suggestion_provider(payload: dict[str, Any], config: AktenfuxConfig) -> dict[str, Any]:
    from aktenfux.llm import suggest_split_boundaries as llm_suggest_split_boundaries  # noqa: PLC0415

    timeout = max(1.0, min(float(config.ollama_timeout), _SUGGEST_TIMEOUT_SECONDS))
    raw = llm_suggest_split_boundaries(
        payload,
        base_url=config.ollama_url,
        model=config.ollama_model,
        timeout=timeout,
    )
    if len(raw.encode("utf-8")) > _SUGGEST_MAX_PAYLOAD_BYTES:
        raise SplitError("Split suggestion response exceeded the allowed size limit.")
    try:
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise SplitError("Split suggestion response was not valid JSON.") from exc


def suggest_split_boundaries(
    document_id: str,
    config: AktenfuxConfig,
    *,
    provider: Callable[[dict[str, Any], AktenfuxConfig], dict[str, Any]] | None = None,
) -> SplitBoundarySuggestion:
    """Return advisory split-boundary suggestions for one validated _Split document."""

    _validate_split_roots(config, include_inbox=False)
    snapshot = _read_snapshot(config, document_id, include_completed=False)
    if snapshot.page_count < 2:
        raise SplitError("Single-page PDFs cannot have split boundaries suggested.")

    page_evidence, extraction_warnings = _bounded_page_evidence(snapshot)
    page_evidence, budget_warnings = _fit_page_evidence_to_request_budget(snapshot, page_evidence)
    extraction_warnings.extend(budget_warnings)
    request_payload = _build_split_suggestion_request_payload(snapshot, page_evidence)

    active_provider = provider or _default_split_suggestion_provider
    try:
        provider_payload = active_provider(request_payload, config)
    except asyncio.CancelledError:
        raise
    except SplitError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SplitError("Split suggestion failed.") from exc

    try:
        provider_response = SplitBoundaryProviderResponse.model_validate(provider_payload)
    except Exception as exc:  # noqa: BLE001
        raise SplitError(
            "The split suggestion provider returned an invalid split suggestion payload."
        ) from exc

    analysed_page_count = len(page_evidence)
    for candidate in provider_response.candidates:
        if candidate.before_page > analysed_page_count:
            raise SplitError(
                "The split suggestion provider returned an invalid split suggestion payload "
                "(boundary outside analysed page range)."
            )
        for evidence in candidate.evidence:
            if evidence.page > analysed_page_count:
                raise SplitError(
                    "The split suggestion provider returned an invalid split suggestion payload "
                    "(evidence outside analysed page range)."
                )

    latest_snapshot = _read_snapshot(config, document_id, include_completed=False)
    if (
        latest_snapshot.source_sha256 != snapshot.source_sha256
        or latest_snapshot.page_count != snapshot.page_count
    ):
        raise SplitError("Split suggestion became stale because the source changed.")

    suggestion_payload = {
        "document_id": snapshot.sidecar.id,
        "source_sha256": snapshot.source_sha256,
        "source_page_count": snapshot.page_count,
        "suggestion_provider": f"ollama:{config.ollama_model}",
        "candidates": [
            candidate.model_dump(mode="json") for candidate in provider_response.candidates
        ],
        "warnings": _bound_combined_suggestion_warnings(
            extraction_warnings, provider_response.warnings
        ),
    }
    try:
        return SplitBoundarySuggestion.model_validate(suggestion_payload)
    except Exception as exc:  # noqa: BLE001
        raise SplitError(
            "The split suggestion provider returned an invalid split suggestion payload."
        ) from exc


def _write_text_atomic(path: Path, text: str) -> None:
    fd: int | None = None
    tmp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        tmp_path.replace(path)
    except OSError as exc:
        raise SplitError(f"Could not persist split metadata for '{path.name}'.") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _write_sidecar_atomic(sidecar: SidecarDocument, sidecar_path: Path) -> None:
    _write_text_atomic(sidecar_path, sidecar.model_dump_json(indent=2))


def _hard_link_is_unsupported(exc: OSError) -> bool:
    unsupported_errnos = {
        errno.EPERM,
        errno.EXDEV,
        errno.ENOSYS,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        errno.EOPNOTSUPP,
    }
    return exc.errno in unsupported_errnos or getattr(exc, "winerror", None) in {1, 50}


def _write_bytes_exclusive_fallback(path: Path, data: bytes) -> None:
    """Create *path* without overwrite when hard-link publication is unavailable."""
    fd: int | None = None
    created = False
    completed = False
    created_identity: tuple[int, int] | None = None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
        created = True
        opened_stat = os.fstat(fd)
        created_identity = (opened_stat.st_dev, opened_stat.st_ino)
        with os.fdopen(fd, "wb") as fh:
            fd = None
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        completed = True
    except FileExistsError as exc:
        raise SplitError(f"Output collision for {path.name}.") from exc
    except OSError as exc:
        raise SplitError(f"Could not write split output '{path.name}'.") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if created and not completed and created_identity is not None:
            try:
                current_stat = path.lstat()
                if (current_stat.st_dev, current_stat.st_ino) == created_identity:
                    path.unlink()
            except OSError:
                pass


def _write_bytes_exclusive(path: Path, data: bytes) -> None:
    fd: int | None = None
    tmp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "wb") as fh:
            fd = None
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp_path, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise SplitError(f"Output collision for {path.name}.") from exc
        except NotImplementedError:
            _write_bytes_exclusive_fallback(path, data)
        except OSError as exc:
            if not _hard_link_is_unsupported(exc):
                raise
            _write_bytes_exclusive_fallback(path, data)
    except OSError as exc:
        raise SplitError(f"Could not write split output '{path.name}'.") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _ensure_directory(path: Path, *, label: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SplitError(f"Could not prepare {label} directory.") from exc


def _sha256_file_bounded(path: Path, *, label: str) -> str:
    try:
        return sha256_file(path)
    except OSError as exc:
        raise SplitError(f"Could not verify {label} '{path.name}'.") from exc


def _part_pdf_bytes(source_bytes: bytes, *, start_page: int, end_page: int) -> bytes:
    reader = PdfReader(BytesIO(source_bytes))
    writer = PdfWriter()
    for page_index in range(start_page - 1, end_page):
        writer.add_page(reader.pages[page_index])
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _validate_plan_against_source(plan: SplitPlan, snapshot: SourceSnapshot) -> list[SplitPartPlan]:
    if snapshot.page_count < 2:
        raise SplitError("Single-page PDFs cannot be split.")

    normalized_boundaries = _normalize_boundaries(plan.boundaries, snapshot.page_count)
    expected_parts = _build_parts(
        Path(plan.source_filename).stem, snapshot.page_count, normalized_boundaries
    )
    expected_operation_id = _deterministic_operation_id(
        snapshot.sidecar.id, snapshot.source_sha256, snapshot.page_count, normalized_boundaries
    )

    if plan.document_id != snapshot.sidecar.id:
        raise SplitError("Split plan document identity mismatch.")
    if not _plan_source_filename_matches_snapshot(
        plan.source_filename,
        snapshot.pdf_name,
        plan.operation_id,
    ):
        raise SplitError("Split plan source filename mismatch.")
    if plan.source_sha256 != snapshot.source_sha256 or plan.source_page_count != snapshot.page_count:
        raise SplitError("Split plan source identity mismatch.")
    if plan.boundaries != normalized_boundaries:
        raise SplitError("Split plan boundaries are not normalized.")
    if plan.parts != expected_parts:
        raise SplitError("Split plan outputs do not match deterministic page ranges.")
    if plan.operation_id != expected_operation_id:
        raise SplitError("Split plan operation ID is invalid for this source.")

    return expected_parts


def _validate_operation_record(
    operation: SplitProvenanceRecord,
    *,
    expected_operation_id: str,
    expected_source_sha256: str,
    expected_page_count: int,
    expected_boundaries: list[int],
    expected_parts: list[SplitPartPlan],
) -> None:
    if operation.operation_id != expected_operation_id:
        raise SplitError("Persisted split operation ID mismatch.")
    if operation.source_sha256 != expected_source_sha256:
        raise SplitError("Persisted split source hash mismatch.")
    if operation.source_page_count != expected_page_count:
        raise SplitError("Persisted split source page count mismatch.")
    if operation.boundaries != expected_boundaries:
        raise SplitError("Persisted split boundaries mismatch.")
    if len(operation.outputs) != len(expected_parts):
        raise SplitError("Persisted split output count mismatch.")
    if operation.state not in {"in_progress", "completed"}:
        raise SplitError("Persisted split operation state is invalid.")

    created_at = _parse_utc(operation.created_at_utc, field_name="created_at_utc")
    completed_at: datetime | None = None
    if operation.completed_at_utc is not None:
        completed_at = _parse_utc(operation.completed_at_utc, field_name="completed_at_utc")

    for output, expected in zip(operation.outputs, expected_parts):
        if output.part_number != expected.part_number:
            raise SplitError("Persisted split part numbering mismatch.")
        if output.start_page != expected.start_page or output.end_page != expected.end_page:
            raise SplitError("Persisted split page ranges mismatch.")
        if output.filename != expected.destination_filename:
            raise SplitError("Persisted split output filenames mismatch.")
        if output.sha256 is not None:
            if len(output.sha256) != 64:
                raise SplitError("Persisted split output hash is malformed.")
            if any(ch not in "0123456789abcdef" for ch in output.sha256):
                raise SplitError("Persisted split output hash is malformed.")

    if operation.state == "completed":
        if completed_at is None:
            raise SplitError("Completed split operation is missing completion timestamp.")
        if completed_at < created_at:
            raise SplitError("Split operation completion timestamp is inconsistent.")
        for output in operation.outputs:
            if output.sha256 is None:
                raise SplitError("Completed split operation is missing output hashes.")


def _staging_paths(config: AktenfuxConfig, operation_id: str) -> tuple[Path, Path]:
    staging_root = config.inbox_path / ".split-staging" / operation_id
    try:
        assert_within_base(staging_root, config.inbox_path)
    except ValueError as exc:
        raise SplitError("Split staging path escaped _Inbox.") from exc
    publish_journal = staging_root / "publish-state.json"
    return staging_root, publish_journal


def _load_publish_journal(
    path: Path, *, expected_outputs: tuple[str, ...] | None = None
) -> PublishJournal | None:
    if path.is_symlink():
        raise SplitError("Split publish journal must not be a symbolic link.")
    if not path.exists():
        return None
    if not path.is_file():
        raise SplitError("Split publish journal is not a regular file.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SplitError("Split publish journal is invalid.") from exc
    if not isinstance(payload, dict):
        raise SplitError("Split publish journal is invalid.")

    state = payload.get("state")
    outputs = payload.get("outputs")
    published = payload.get("published")
    if state not in {"publishing", "completed"}:
        raise SplitError("Split publish journal state is invalid.")
    if not isinstance(outputs, list) or not outputs or not all(
        isinstance(item, str) for item in outputs
    ):
        raise SplitError("Split publish journal outputs are invalid.")
    if len(set(outputs)) != len(outputs) or any(
        not item or "/" in item or "\\" in item or Path(item).suffix.lower() != ".pdf"
        for item in outputs
    ):
        raise SplitError("Split publish journal outputs are invalid.")
    if not isinstance(published, list) or not all(isinstance(item, str) for item in published):
        raise SplitError("Split publish journal published set is invalid.")
    if len(set(published)) != len(published) or not set(published).issubset(outputs):
        raise SplitError("Split publish journal published set is invalid.")
    if state == "completed" and set(published) != set(outputs):
        raise SplitError("Completed split publish journal is incomplete.")

    journal = PublishJournal(
        state=state,
        outputs=tuple(outputs),
        published=frozenset(published),
    )
    if expected_outputs is not None and journal.outputs != expected_outputs:
        raise SplitError("Split publish journal outputs do not match the split plan.")
    return journal


def _write_publish_journal(
    path: Path,
    *,
    state: str,
    outputs: tuple[str, ...],
    published: set[str] | frozenset[str],
) -> None:
    if state not in {"publishing", "completed"}:
        raise SplitError("Split publish journal state is invalid.")
    if not set(published).issubset(outputs):
        raise SplitError("Split publish journal published set is invalid.")
    if state == "completed" and set(published) != set(outputs):
        raise SplitError("Completed split publish journal is incomplete.")
    payload = {
        "state": state,
        "outputs": list(outputs),
        "published": sorted(published),
    }
    _write_text_atomic(path, json.dumps(payload, indent=2))


def is_split_output_pending(pdf_path: Path, inbox_root: Path) -> bool:
    """Return whether *pdf_path* belongs to an incompletely published split batch."""
    staging_base = inbox_root / ".split-staging"
    if staging_base.is_symlink():
        raise SplitError("Split staging root must not be a symbolic link.")
    if not staging_base.exists():
        return False
    if not staging_base.is_dir():
        raise SplitError("Split staging root is not a directory.")

    try:
        operation_dirs = list(staging_base.iterdir())
    except OSError as exc:
        raise SplitError("Could not inspect split publish state.") from exc
    for operation_dir in operation_dirs:
        if operation_dir.is_symlink() or not operation_dir.is_dir():
            raise SplitError("Split staging operation path is invalid.")
        journal = _load_publish_journal(operation_dir / "publish-state.json")
        if journal is None:
            continue
        if journal.state == "publishing" and pdf_path.name in journal.outputs:
            return True
    return False


def _cleanup_staging(
    staging_root: Path,
    publish_journal_path: Path,
    *,
    expected_outputs: tuple[str, ...],
) -> None:
    if staging_root.is_symlink():
        raise SplitError("Split staging operation path must not be a symbolic link.")
    if not staging_root.exists():
        return
    if not staging_root.is_dir():
        raise SplitError("Split staging operation path is not a directory.")

    allowed = {*expected_outputs, publish_journal_path.name}
    try:
        children = list(staging_root.iterdir())
        for child in children:
            if child.name not in allowed or child.is_symlink() or not child.is_file():
                raise SplitError("Split staging contains an unexpected artifact; cleanup stopped.")
        for child in children:
            child.unlink()
        staging_root.rmdir()
        try:
            staging_root.parent.rmdir()
        except OSError:
            pass
    except SplitError:
        raise
    except OSError as exc:
        raise SplitError("Could not remove completed split staging data.") from exc


def _ensure_staged_output(
    staging_path: Path,
    *,
    expected_hash: str,
    part_bytes: bytes | None,
    output_name: str,
) -> None:
    if staging_path.exists():
        if staging_path.is_symlink() or not staging_path.is_file():
            raise SplitError(f"Staged split output '{output_name}' is invalid.")
        if _sha256_file_bounded(staging_path, label="staged split output") != expected_hash:
            raise SplitError(f"Staged split output hash mismatch for '{output_name}'.")
        return
    if part_bytes is None:
        raise SplitError(f"Missing split bytes for '{output_name}'.")
    _write_bytes_exclusive(staging_path, part_bytes)
    if _sha256_file_bounded(staging_path, label="staged split output") != expected_hash:
        raise SplitError(f"Split staging hash verification failed for '{output_name}'.")


def _publish_output_from_stage(
    staged_path: Path, final_path: Path, *, expected_hash: str, inbox_root: Path
) -> None:
    _validate_direct_destination_path(final_path, root=inbox_root)
    if final_path.exists():
        if _sha256_file_bounded(final_path, label="split output") != expected_hash:
            raise SplitError(f"Destination collision for {final_path.name}.")
        return

    try:
        data = staged_path.read_bytes()
    except OSError as exc:
        raise SplitError(f"Could not read staged output '{staged_path.name}'.") from exc
    _write_bytes_exclusive(final_path, data)
    if _sha256_file_bounded(final_path, label="split output") != expected_hash:
        raise SplitError(f"Split output hash verification failed for '{final_path.name}'.")


def _move_file_no_overwrite(source: Path, destination: Path, *, label: str) -> None:
    if destination.exists():
        raise SplitError(f"Destination collision for retained completed source {destination.name}.")
    if source.is_symlink() or not source.is_file():
        raise SplitError(f"Retained completed source {label} is missing or invalid.")
    try:
        source.replace(destination)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise SplitError(f"Could not move retained completed source {label}.") from exc
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise SplitError(f"Could not read retained completed source {label}.") from exc
    _write_bytes_exclusive(destination, data)
    try:
        source.unlink()
    except OSError as exc:
        raise SplitError(f"Could not remove source {label} after copy.") from exc


def _resolve_completed_transition_state(
    *,
    source_pdf: Path,
    source_sidecar: Path,
    retained_pdf: Path,
    retained_sidecar: Path,
) -> str:
    source_pdf_exists = source_pdf.exists()
    source_sidecar_exists = source_sidecar.exists()
    retained_pdf_exists = retained_pdf.exists()
    retained_sidecar_exists = retained_sidecar.exists()
    if source_pdf_exists and source_sidecar_exists and not retained_pdf_exists and not retained_sidecar_exists:
        return "initial"
    if not source_pdf_exists and source_sidecar_exists and retained_pdf_exists and not retained_sidecar_exists:
        return "pdf_moved"
    if not source_pdf_exists and not source_sidecar_exists and retained_pdf_exists and retained_sidecar_exists:
        return "completed"
    raise SplitError("Retained completed source is in an ambiguous recovery state.")


def _verify_completed_sidecar_identity(
    sidecar_path: Path, *, document_id: str, source_sha256: str, operation_id: str
) -> SidecarDocument:
    sidecar = _read_sidecar_validated(sidecar_path)
    if sidecar.id != document_id or sidecar.sha256 != source_sha256:
        raise SplitError("Retained completed source sidecar identity mismatch.")
    operation = _completed_operation_for_document(sidecar, operation_id=operation_id)
    if operation is None:
        raise SplitError("Retained completed source sidecar is missing completed split provenance.")
    return sidecar


def _retain_completed_source(
    *,
    config: AktenfuxConfig,
    snapshot: SourceSnapshot,
    operation: SplitProvenanceRecord,
) -> tuple[Path, Path]:
    imported_root = _validate_completed_roots(config, include_inbox=True)
    retained_pdf, retained_sidecar = _completed_destination_paths(
        imported_root=imported_root,
        source_pdf_name=snapshot.pdf_name,
        operation_id=operation.operation_id,
    )
    _validate_direct_destination_path(retained_pdf, root=imported_root)
    try:
        assert_within_base(retained_sidecar, imported_root)
    except ValueError as exc:
        raise SplitError(f"{retained_sidecar.name} is outside the expected destination root.") from exc
    if retained_sidecar.parent.resolve() != imported_root.resolve():
        raise SplitError(f"{retained_sidecar.name} must be written directly to {imported_root.name}.")
    if retained_sidecar.suffix.lower() != ".json":
        raise SplitError(f"{retained_sidecar.name} has invalid extension; expected .json.")

    source_pdf = config.split_path / snapshot.pdf_name
    source_sidecar = sidecar_path_for(source_pdf)
    if source_pdf.exists():
        _validate_direct_regular_file(source_pdf, root=config.split_path, suffix=".pdf")
    if source_sidecar.exists():
        _validate_direct_regular_file(source_sidecar, root=config.split_path, suffix=".json")
    if retained_pdf.exists():
        _validate_direct_regular_file(retained_pdf, root=imported_root, suffix=".pdf")
    if retained_sidecar.exists():
        _validate_direct_regular_file(retained_sidecar, root=imported_root, suffix=".json")

    state = _resolve_completed_transition_state(
        source_pdf=source_pdf,
        source_sidecar=source_sidecar,
        retained_pdf=retained_pdf,
        retained_sidecar=retained_sidecar,
    )
    _ensure_directory(imported_root, label="imported")
    if state == "initial":
        if _sha256_file_bounded(source_pdf, label="split source") != snapshot.source_sha256:
            raise SplitError("Source PDF hash changed before completed-source transition.")
        _verify_completed_sidecar_identity(
            source_sidecar,
            document_id=snapshot.sidecar.id,
            source_sha256=snapshot.source_sha256,
            operation_id=operation.operation_id,
        )
        _move_file_no_overwrite(source_pdf, retained_pdf, label="PDF")
        state = _resolve_completed_transition_state(
            source_pdf=source_pdf,
            source_sidecar=source_sidecar,
            retained_pdf=retained_pdf,
            retained_sidecar=retained_sidecar,
        )
    if state == "pdf_moved":
        if _sha256_file_bounded(retained_pdf, label="retained split source") != snapshot.source_sha256:
            raise SplitError("Retained completed source PDF no longer matches authoritative hash.")
        _verify_completed_sidecar_identity(
            source_sidecar,
            document_id=snapshot.sidecar.id,
            source_sha256=snapshot.source_sha256,
            operation_id=operation.operation_id,
        )
        _move_file_no_overwrite(source_sidecar, retained_sidecar, label="sidecar")
        state = _resolve_completed_transition_state(
            source_pdf=source_pdf,
            source_sidecar=source_sidecar,
            retained_pdf=retained_pdf,
            retained_sidecar=retained_sidecar,
        )
    if state != "completed":
        raise SplitError("Retained completed-source transition did not complete.")

    if _sha256_file_bounded(retained_pdf, label="retained split source") != snapshot.source_sha256:
        raise SplitError("Retained completed source PDF no longer matches authoritative hash.")
    retained_sidecar_doc = _verify_completed_sidecar_identity(
        retained_sidecar,
        document_id=snapshot.sidecar.id,
        source_sha256=snapshot.source_sha256,
        operation_id=operation.operation_id,
    )
    retained_path_text = str(retained_pdf.resolve())
    if retained_sidecar_doc.current_path != retained_path_text:
        retained_sidecar_doc.current_path = retained_path_text
        _write_sidecar_atomic(retained_sidecar_doc, retained_sidecar)
    return retained_pdf, retained_sidecar


def list_completed_split_documents(config: AktenfuxConfig) -> list[CompletedSplitSummary]:
    """Return retained completed split sources from _Imported."""
    imported_root = _validate_completed_roots(config, include_inbox=False)
    if not imported_root.exists():
        return []
    entries: list[CompletedSplitSummary] = []
    seen_ids: set[str] = set()
    for pdf_path in sorted(imported_root.glob("*.pdf")):
        try:
            _validate_direct_regular_file(pdf_path, root=imported_root, suffix=".pdf")
            sidecar_path = sidecar_path_for(pdf_path)
            _validate_direct_regular_file(sidecar_path, root=imported_root, suffix=".json")
            sidecar = _read_sidecar_validated(sidecar_path)
            operation = _completed_operation_for_document(sidecar)
            if operation is None:
                continue
            _validate_operation_record(
                operation,
                expected_operation_id=operation.operation_id,
                expected_source_sha256=sidecar.sha256,
                expected_page_count=operation.source_page_count,
                expected_boundaries=operation.boundaries,
                expected_parts=[
                    SplitPartPlan(
                        part_number=item.part_number,
                        start_page=item.start_page,
                        end_page=item.end_page,
                        destination_filename=item.filename,
                    )
                    for item in operation.outputs
                ],
            )
            if sidecar.id in seen_ids:
                raise SplitError("Duplicate document id found in retained completed sources.")
            seen_ids.add(sidecar.id)
            entries.append(
                CompletedSplitSummary(
                    document_id=sidecar.id,
                    retained_pdf=pdf_path.name,
                    source_sha256=sidecar.sha256,
                    operation_id=operation.operation_id,
                    completed_at_utc=operation.completed_at_utc or operation.created_at_utc,
                    outputs=tuple(item.filename for item in operation.outputs),
                )
            )
        except SplitError:
            continue
    return entries


def reconcile_completed_split_sources(
    config: AktenfuxConfig,
    *,
    apply_changes: bool,
) -> CompletedSplitReconcileResult:
    """Dry-run-first reconciliation for completed split sources still in _Split."""
    _validate_completed_roots(config, include_inbox=True)
    eligible: list[CompletedSplitSummary] = []
    transitioned: list[CompletedSplitSummary] = []
    issues: list[CompletedSplitReconcileIssue] = []

    if not config.split_path.exists():
        return CompletedSplitReconcileResult(
            dry_run=not apply_changes,
            eligible=tuple(),
            transitioned=tuple(),
            issues=tuple(),
        )

    for pdf_path in sorted(config.split_path.glob("*.pdf")):
        document_id = "(unknown)"
        try:
            _validate_direct_regular_file(pdf_path, root=config.split_path, suffix=".pdf")
            sidecar_path = sidecar_path_for(pdf_path)
            _validate_direct_regular_file(sidecar_path, root=config.split_path, suffix=".json")
            sidecar = _read_sidecar_validated(sidecar_path)
            document_id = sidecar.id
            operation = _completed_operation_for_document(sidecar)
            if operation is None:
                continue
            if len([op for op in sidecar.split_operations if op.state == "completed"]) != 1:
                raise SplitError("multiple completed split operations")
            expected_parts = [
                SplitPartPlan(
                    part_number=item.part_number,
                    start_page=item.start_page,
                    end_page=item.end_page,
                    destination_filename=item.filename,
                )
                for item in operation.outputs
            ]
            _validate_operation_record(
                operation,
                expected_operation_id=operation.operation_id,
                expected_source_sha256=sidecar.sha256,
                expected_page_count=operation.source_page_count,
                expected_boundaries=operation.boundaries,
                expected_parts=expected_parts,
            )
            snapshot = _read_validated_snapshot(
                pdf_path,
                sidecar_path,
                sidecar,
                lifecycle_root=config.split_path,
                lifecycle_label="split",
            )
            if snapshot.source_sha256 != sidecar.sha256:
                raise SplitError("sidecar/source hash mismatch")
            for output in operation.outputs:
                if output.sha256 is None:
                    raise SplitError(f"missing output hash for {output.filename}")
                final_path = config.inbox_path / output.filename
                _validate_direct_destination_path(final_path, root=config.inbox_path)
                if not final_path.exists():
                    raise SplitError(f"missing output {output.filename}")
                if _sha256_file_bounded(final_path, label="completed split output") != output.sha256:
                    raise SplitError(f"output hash mismatch for {output.filename}")
            summary = CompletedSplitSummary(
                document_id=sidecar.id,
                retained_pdf=_completed_destination_paths(
                    imported_root=config.imported_path,
                    source_pdf_name=pdf_path.name,
                    operation_id=operation.operation_id,
                )[0].name,
                source_sha256=sidecar.sha256,
                operation_id=operation.operation_id,
                completed_at_utc=operation.completed_at_utc or operation.created_at_utc,
                outputs=tuple(item.filename for item in operation.outputs),
            )
            eligible.append(summary)
            if apply_changes:
                _retain_completed_source(config=config, snapshot=snapshot, operation=operation)
                transitioned.append(summary)
        except SplitError as exc:
            issues.append(
                CompletedSplitReconcileIssue(
                    document_id=document_id,
                    source_pdf=pdf_path.name,
                    reason=str(exc),
                )
            )

    return CompletedSplitReconcileResult(
        dry_run=not apply_changes,
        eligible=tuple(eligible),
        transitioned=tuple(transitioned),
        issues=tuple(issues),
    )

def plan_split(document_id: str, boundaries: list[int], config: AktenfuxConfig) -> SplitPlan:
    """Return a read-only split plan for a document staged in _Split."""
    _validate_split_roots(config, include_inbox=False)
    snapshot = _read_snapshot(config, document_id, include_completed=False)
    if snapshot.page_count < 2:
        raise SplitError("Single-page PDFs cannot be split.")

    normalized_boundaries = _normalize_boundaries(boundaries, snapshot.page_count)
    parts = _build_parts(snapshot.pdf_path.stem, snapshot.page_count, normalized_boundaries)
    operation_id = _deterministic_operation_id(
        snapshot.sidecar.id, snapshot.source_sha256, snapshot.page_count, normalized_boundaries
    )
    return SplitPlan(
        operation_id=operation_id,
        document_id=snapshot.sidecar.id,
        source_filename=snapshot.pdf_name,
        source_sha256=snapshot.source_sha256,
        source_page_count=snapshot.page_count,
        boundaries=normalized_boundaries,
        parts=parts,
    )


def execute_split(plan: SplitPlan, config: AktenfuxConfig) -> SplitResult:
    """Execute a split plan with recoverable, idempotent semantics."""
    _validate_split_roots(config, include_inbox=True)
    snapshot = _read_snapshot(
        config,
        plan.document_id,
        include_completed=True,
        operation_id=plan.operation_id,
    )
    expected_parts = _validate_plan_against_source(plan, snapshot)
    expected_operation_id = _deterministic_operation_id(
        snapshot.sidecar.id, snapshot.source_sha256, snapshot.page_count, plan.boundaries
    )

    sidecar = snapshot.sidecar
    operation_index = next(
        (
            idx
            for idx, candidate in enumerate(sidecar.split_operations)
            if candidate.operation_id == expected_operation_id
        ),
        None,
    )

    if operation_index is None:
        operation = SplitProvenanceRecord(
            operation_id=expected_operation_id,
            source_sha256=snapshot.source_sha256,
            source_page_count=snapshot.page_count,
            boundaries=plan.boundaries,
            outputs=[
                SplitOutputRecord(
                    part_number=part.part_number,
                    start_page=part.start_page,
                    end_page=part.end_page,
                    filename=part.destination_filename,
                )
                for part in expected_parts
            ],
            state="in_progress",
            created_at_utc=_utc_now(),
        )
        sidecar.split_operations.append(operation)
        _write_sidecar_atomic(sidecar, snapshot.sidecar_path)
        operation_index = len(sidecar.split_operations) - 1

    operation = sidecar.split_operations[operation_index]
    _validate_operation_record(
        operation,
        expected_operation_id=expected_operation_id,
        expected_source_sha256=snapshot.source_sha256,
        expected_page_count=snapshot.page_count,
        expected_boundaries=plan.boundaries,
        expected_parts=expected_parts,
    )

    expected_output_names = tuple(output.filename for output in operation.outputs)
    staging_root, publish_journal_path = _staging_paths(config, expected_operation_id)
    journal = _load_publish_journal(
        publish_journal_path,
        expected_outputs=expected_output_names,
    )

    if operation.state == "completed":
        if journal is not None:
            if set(journal.published) != set(expected_output_names):
                raise SplitError("Completed split operation has an incomplete publish journal.")
            _write_publish_journal(
                publish_journal_path,
                state="completed",
                outputs=expected_output_names,
                published=journal.published,
            )
        _cleanup_staging(
            staging_root,
            publish_journal_path,
            expected_outputs=expected_output_names,
        )
        completed_outputs: list[SplitPartResult] = []
        for output in operation.outputs:
            final_path = config.inbox_path / output.filename
            _validate_direct_destination_path(final_path, root=config.inbox_path)
            if not final_path.exists():
                raise SplitError(
                    f"Completed split output '{output.filename}' is missing; refusing to republish."
                )
            if output.sha256 is None or _sha256_file_bounded(
                final_path, label="completed split output"
            ) != output.sha256:
                raise SplitError(f"Completed split output '{output.filename}' no longer matches provenance.")
            completed_outputs.append(
                SplitPartResult(
                    part_number=output.part_number,
                    start_page=output.start_page,
                    end_page=output.end_page,
                    filename=output.filename,
                    sha256=output.sha256,
                )
            )
        if snapshot.lifecycle_label == "split":
            _retain_completed_source(config=config, snapshot=snapshot, operation=operation)
        return SplitResult(
            operation_id=operation.operation_id,
            document_id=plan.document_id,
            source_filename=plan.source_filename,
            source_sha256=snapshot.source_sha256,
            source_page_count=snapshot.page_count,
            boundaries=plan.boundaries,
            outputs=completed_outputs,
            completed_at_utc=operation.completed_at_utc or _utc_now(),
        )

    if journal is not None and journal.state != "publishing":
        raise SplitError("In-progress split operation has an invalid publish journal state.")
    published = set(journal.published) if journal is not None else set()
    _ensure_directory(config.inbox_path, label="inbox")
    _ensure_directory(staging_root, label="split staging")

    for output_index, output in enumerate(operation.outputs):
        part_bytes: bytes | None = None
        if output.sha256 is None:
            part_bytes = _part_pdf_bytes(
                snapshot.source_bytes, start_page=output.start_page, end_page=output.end_page
            )
            part_hash = hashlib.sha256(part_bytes).hexdigest()
            updated_outputs = list(operation.outputs)
            updated_outputs[output_index] = output.model_copy(update={"sha256": part_hash})
            operation = operation.model_copy(update={"outputs": updated_outputs})
            sidecar.split_operations[operation_index] = operation
            _write_sidecar_atomic(sidecar, snapshot.sidecar_path)
            output = operation.outputs[output_index]
        if output.sha256 is None:
            raise SplitError(f"Missing split output hash for '{output.filename}'.")

        staged_path = staging_root / output.filename
        if part_bytes is None and not staged_path.exists():
            part_bytes = _part_pdf_bytes(
                snapshot.source_bytes, start_page=output.start_page, end_page=output.end_page
            )
            part_hash = hashlib.sha256(part_bytes).hexdigest()
            if part_hash != output.sha256:
                raise SplitError(f"Persisted split hash mismatch for '{output.filename}'.")
        _ensure_staged_output(
            staged_path,
            expected_hash=output.sha256,
            part_bytes=part_bytes,
            output_name=output.filename,
        )

    # The scanner treats every output named by this durable marker as unavailable
    # until the whole batch is published and the marker is completed or removed.
    _write_publish_journal(
        publish_journal_path,
        state="publishing",
        outputs=expected_output_names,
        published=published,
    )

    for output in operation.outputs:
        if output.sha256 is None:
            raise SplitError(f"Missing split output hash for '{output.filename}'.")
        final_path = config.inbox_path / output.filename
        staged_path = staging_root / output.filename
        _validate_direct_destination_path(final_path, root=config.inbox_path)

        if output.filename in published:
            if not final_path.exists():
                raise SplitError(
                    f"Split output '{output.filename}' was previously published and is now missing; refusing to republish."
                )
            if _sha256_file_bounded(final_path, label="published split output") != output.sha256:
                raise SplitError(f"Published split output '{output.filename}' no longer matches provenance.")
            continue

        _publish_output_from_stage(
            staged_path,
            final_path,
            expected_hash=output.sha256,
            inbox_root=config.inbox_path,
        )
        published.add(output.filename)
        _write_publish_journal(
            publish_journal_path,
            state="publishing",
            outputs=expected_output_names,
            published=published,
        )

    if set(published) != set(expected_output_names):
        raise SplitError("Split publish journal is incomplete after publication.")
    for output in operation.outputs:
        if output.sha256 is None:
            raise SplitError(f"Missing split output hash for '{output.filename}'.")
        final_path = config.inbox_path / output.filename
        if not final_path.is_file() or final_path.is_symlink():
            raise SplitError(f"Published split output '{output.filename}' is missing or invalid.")
        if _sha256_file_bounded(final_path, label="published split output") != output.sha256:
            raise SplitError(f"Published split output '{output.filename}' no longer matches provenance.")

    operation = operation.model_copy(
        update={
            "state": "completed",
            "completed_at_utc": operation.completed_at_utc or _utc_now(),
        }
    )
    sidecar.split_operations[operation_index] = operation
    _write_sidecar_atomic(sidecar, snapshot.sidecar_path)
    _write_publish_journal(
        publish_journal_path,
        state="completed",
        outputs=expected_output_names,
        published=published,
    )
    _cleanup_staging(
        staging_root,
        publish_journal_path,
        expected_outputs=expected_output_names,
    )
    _retain_completed_source(config=config, snapshot=snapshot, operation=operation)

    outputs: list[SplitPartResult] = []
    for output in operation.outputs:
        if output.sha256 is None:
            raise SplitError(f"Incomplete split provenance for {output.filename}.")
        outputs.append(
            SplitPartResult(
                part_number=output.part_number,
                start_page=output.start_page,
                end_page=output.end_page,
                filename=output.filename,
                sha256=output.sha256,
            )
        )

    return SplitResult(
        operation_id=operation.operation_id,
        document_id=plan.document_id,
        source_filename=plan.source_filename,
        source_sha256=snapshot.source_sha256,
        source_page_count=snapshot.page_count,
        boundaries=plan.boundaries,
        outputs=outputs,
        completed_at_utc=operation.completed_at_utc or _utc_now(),
    )
