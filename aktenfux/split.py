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
_SPLIT_OPERATION_ID_PATTERN = re.compile(r"[a-f0-9]{32}")
_WORKBENCH_DISPLAY_NAME_MAX_CHARS = 200
_SUGGEST_MAX_PAGES = 64
_SUGGEST_MAX_CHARS_PER_PAGE = 1200
_SUGGEST_MAX_PAYLOAD_BYTES = 80_000
_SUGGEST_MAX_WARNINGS = 32
_SUGGEST_TIMEOUT_SECONDS = 120.0
_COMPLETED_TRANSITION_SCHEMA_VERSION = 1
_COMPLETED_TRANSITION_STATES = (
    "planned",
    "pdf_staged",
    "sidecar_staged",
    "retained_pdf_published",
    "retained_sidecar_published",
    "source_pdf_removed",
    "source_sidecar_removed",
    "finalized",
)


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
class CompletedSplitListIssue:
    retained_pdf: str
    reason: str


@dataclass(frozen=True)
class CompletedSplitListResult:
    entries: tuple[CompletedSplitSummary, ...]
    issues: tuple[CompletedSplitListIssue, ...]


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
class CompletedTransitionJournal:
    operation_id: str
    document_id: str
    source_sha256: str
    source_pdf: str
    source_sidecar: str
    retained_pdf: str
    retained_sidecar: str
    state: str


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


def _validate_operation_id_format(operation_id: str) -> None:
    if not _SPLIT_OPERATION_ID_PATTERN.fullmatch(operation_id):
        raise SplitError("Split operation ID format is invalid.")


def _assert_not_hard_link(path: Path, *, label: str) -> None:
    try:
        stat_result = path.lstat()
    except OSError as exc:
        raise SplitError(f"Could not inspect {label}.") from exc
    if getattr(stat_result, "st_nlink", 1) > 1:
        raise SplitError(f"{label} must not be a hard link.")


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
    _assert_not_hard_link(path, label=path.name)


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
        sidecar_path = sidecar_path_for(pdf_path)
        try:
            _validate_direct_regular_file(sidecar_path, root=root, suffix=".json")
            sidecar = _read_sidecar_validated(sidecar_path)
        except SplitError as exc:
            logger.warning("Skipping invalid %s candidate '%s': %s", root_label, pdf_path.name, exc)
            continue
        if sidecar.id != document_id:
            continue
        try:
            _validate_direct_regular_file(pdf_path, root=root, suffix=".pdf")
        except SplitError as exc:
            raise SplitError(f"Authoritative source for document '{document_id}' is invalid.") from exc
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


def _completed_transition_root(imported_root: Path) -> Path:
    root = imported_root / ".split-completed-transition"
    try:
        assert_within_base(root, imported_root)
    except ValueError as exc:
        raise SplitError("Split completed transition path escaped _Imported.") from exc
    return root


def _completed_transition_paths(
    *,
    imported_root: Path,
    operation_id: str,
) -> tuple[Path, Path, Path]:
    _validate_operation_id_format(operation_id)
    transition_root = _completed_transition_root(imported_root)
    operation_root = transition_root / operation_id
    journal_path = operation_root / "transition-state.json"
    return operation_root, transition_root, journal_path


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True))


def _load_completed_transition_journal(journal_path: Path) -> CompletedTransitionJournal | None:
    if journal_path.is_symlink():
        raise SplitError("Completed transition journal must not be a symbolic link.")
    if not journal_path.exists():
        return None
    if not journal_path.is_file():
        raise SplitError("Completed transition journal is not a regular file.")
    try:
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SplitError("Completed transition journal is invalid.") from exc
    if not isinstance(payload, dict):
        raise SplitError("Completed transition journal is invalid.")
    if payload.get("schema_version") != _COMPLETED_TRANSITION_SCHEMA_VERSION:
        raise SplitError("Completed transition journal schema version is invalid.")

    required_fields = {
        "operation_id",
        "document_id",
        "source_sha256",
        "source_pdf",
        "source_sidecar",
        "retained_pdf",
        "retained_sidecar",
        "state",
    }
    if not required_fields.issubset(payload):
        raise SplitError("Completed transition journal is missing required fields.")
    state = payload["state"]
    if state not in _COMPLETED_TRANSITION_STATES:
        raise SplitError("Completed transition journal state is invalid.")
    operation_id = str(payload["operation_id"])
    _validate_operation_id_format(operation_id)
    for name_key, expected_suffix in (
        ("source_pdf", ".pdf"),
        ("source_sidecar", ".json"),
        ("retained_pdf", ".pdf"),
        ("retained_sidecar", ".json"),
    ):
        value = str(payload[name_key])
        candidate = Path(value)
        if candidate.is_absolute() or candidate.name != value or candidate.suffix.lower() != expected_suffix:
            raise SplitError("Completed transition journal file names are invalid.")
    return CompletedTransitionJournal(
        operation_id=operation_id,
        document_id=str(payload["document_id"]),
        source_sha256=str(payload["source_sha256"]),
        source_pdf=str(payload["source_pdf"]),
        source_sidecar=str(payload["source_sidecar"]),
        retained_pdf=str(payload["retained_pdf"]),
        retained_sidecar=str(payload["retained_sidecar"]),
        state=state,
    )


def _write_completed_transition_journal(
    journal_path: Path,
    journal: CompletedTransitionJournal,
    *,
    state: str,
) -> CompletedTransitionJournal:
    if state not in _COMPLETED_TRANSITION_STATES:
        raise SplitError("Completed transition journal state is invalid.")
    _validate_operation_id_format(journal.operation_id)
    updated = CompletedTransitionJournal(
        operation_id=journal.operation_id,
        document_id=journal.document_id,
        source_sha256=journal.source_sha256,
        source_pdf=journal.source_pdf,
        source_sidecar=journal.source_sidecar,
        retained_pdf=journal.retained_pdf,
        retained_sidecar=journal.retained_sidecar,
        state=state,
    )
    _write_json_atomic(
        journal_path,
        {
            "schema_version": _COMPLETED_TRANSITION_SCHEMA_VERSION,
            "operation_id": updated.operation_id,
            "document_id": updated.document_id,
            "source_sha256": updated.source_sha256,
            "source_pdf": updated.source_pdf,
            "source_sidecar": updated.source_sidecar,
            "retained_pdf": updated.retained_pdf,
            "retained_sidecar": updated.retained_sidecar,
            "state": updated.state,
        },
    )
    return updated


def _read_completed_transition_journal_for_operation(
    *,
    imported_root: Path,
    operation_id: str,
) -> CompletedTransitionJournal | None:
    _validate_operation_id_format(operation_id)
    operation_root, _, journal_path = _completed_transition_paths(
        imported_root=imported_root,
        operation_id=operation_id,
    )
    if operation_root.is_symlink():
        raise SplitError("Completed transition operation path must not be a symbolic link.")
    journal = _load_completed_transition_journal(journal_path)
    if journal is not None and journal.operation_id != operation_id:
        raise SplitError("Completed transition journal operation ID mismatch.")
    return journal


def _list_completed_transition_journals(imported_root: Path) -> list[CompletedTransitionJournal]:
    transition_root = _completed_transition_root(imported_root)
    if transition_root.is_symlink():
        raise SplitError("Completed transition root must not be a symbolic link.")
    if not transition_root.exists():
        return []
    if not transition_root.is_dir():
        raise SplitError("Completed transition root is not a directory.")
    journals: list[CompletedTransitionJournal] = []
    for operation_root in sorted(transition_root.iterdir()):
        if operation_root.is_symlink() or not operation_root.is_dir():
            raise SplitError("Completed transition operation path is invalid.")
        _validate_operation_id_format(operation_root.name)
        journal = _load_completed_transition_journal(operation_root / "transition-state.json")
        if journal is None:
            continue
        if journal.operation_id != operation_root.name:
            raise SplitError("Completed transition journal operation ID mismatch.")
        journals.append(journal)
    return journals


def _cleanup_completed_transition_artifacts(
    *,
    operation_root: Path,
    transition_root: Path,
) -> None:
    if operation_root.is_symlink():
        raise SplitError("Completed transition operation path must not be a symbolic link.")
    if not operation_root.exists():
        return
    if not operation_root.is_dir():
        raise SplitError("Completed transition operation path is not a directory.")
    allowed = {"transition-state.json", "source.pdf", "source.json"}

    def _is_owned_atomic_temp(path: Path) -> bool:
        return path.name.startswith(".transition-state.json.") and path.name.endswith(".tmp")

    try:
        children = list(operation_root.iterdir())
        for child in children:
            if child.is_symlink() or not child.is_file():
                raise SplitError("Completed transition staging contains unexpected artifacts.")
            if child.name not in allowed and not _is_owned_atomic_temp(child):
                raise SplitError("Completed transition staging contains unexpected artifacts.")
        for child in children:
            child.unlink()
        operation_root.rmdir()
        try:
            transition_root.rmdir()
        except OSError:
            pass
    except SplitError:
        raise
    except OSError as exc:
        raise SplitError("Could not clean completed transition staging data.") from exc


def _plan_source_filename_matches_snapshot(plan_source_filename: str, snapshot_name: str, operation_id: str) -> bool:
    if plan_source_filename == snapshot_name:
        return True
    suffix = f"{_completed_suffix(operation_id)}.pdf"
    if not snapshot_name.endswith(suffix):
        return False
    derived = f"{snapshot_name[: -len(suffix)]}.pdf"
    return plan_source_filename == derived


def _derive_original_source_pdf_name_from_retained(retained_pdf_name: str, operation_id: str) -> str:
    suffix = f"{_completed_suffix(operation_id)}.pdf"
    if not retained_pdf_name.endswith(suffix):
        raise SplitError("Retained completed source filename does not match operation ID suffix.")
    return f"{retained_pdf_name[: -len(suffix)]}.pdf"


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


def _read_snapshot_from_transition_journal(
    *,
    config: AktenfuxConfig,
    journal: CompletedTransitionJournal,
    expected_document_id: str,
    expected_operation_id: str,
) -> SourceSnapshot:
    if journal.document_id != expected_document_id:
        raise SplitError("Completed transition journal document ID mismatch.")
    if journal.operation_id != expected_operation_id:
        raise SplitError("Completed transition journal operation ID mismatch.")
    source_pdf = config.split_path / journal.source_pdf
    source_sidecar = config.split_path / journal.source_sidecar
    retained_pdf = config.imported_path / journal.retained_pdf
    retained_sidecar = config.imported_path / journal.retained_sidecar

    pdf_path: Path | None = None
    sidecar_path: Path | None = None
    lifecycle_root: Path | None = None
    lifecycle_label: str | None = None

    if source_sidecar.exists():
        _validate_direct_regular_file(source_sidecar, root=config.split_path, suffix=".json")
        sidecar_path = source_sidecar
        lifecycle_root = config.split_path
        lifecycle_label = "split"
    elif retained_sidecar.exists():
        _validate_direct_regular_file(retained_sidecar, root=config.imported_path, suffix=".json")
        sidecar_path = retained_sidecar
        lifecycle_root = config.imported_path
        lifecycle_label = "imported"
    else:
        raise SplitError("Completed transition sidecar is missing from both split and imported roots.")

    if source_pdf.exists():
        _validate_direct_regular_file(source_pdf, root=config.split_path, suffix=".pdf")
        pdf_path = source_pdf
        lifecycle_root = config.split_path
        lifecycle_label = "split"
    elif retained_pdf.exists():
        _validate_direct_regular_file(retained_pdf, root=config.imported_path, suffix=".pdf")
        pdf_path = retained_pdf
        lifecycle_root = config.imported_path
        lifecycle_label = "imported"
    else:
        raise SplitError("Completed transition PDF is missing from both split and imported roots.")

    if sidecar_path is None or pdf_path is None or lifecycle_root is None or lifecycle_label is None:
        raise SplitError("Completed transition journal could not resolve source identity.")
    sidecar = _read_sidecar_validated(sidecar_path)
    completed = _completed_operation_for_document(sidecar, operation_id=journal.operation_id)
    if completed is None:
        raise SplitError("Completed transition sidecar is missing completed split provenance.")
    if sidecar.sha256 != journal.source_sha256:
        raise SplitError("Completed transition journal source hash mismatch.")
    return _read_validated_snapshot(
        pdf_path,
        sidecar_path,
        sidecar,
        lifecycle_root=lifecycle_root,
        lifecycle_label=lifecycle_label,
    )


def _read_snapshot(
    config: AktenfuxConfig,
    document_id: str,
    *,
    include_completed: bool,
    operation_id: str | None = None,
) -> SourceSnapshot:
    imported_root = _validate_imported_root(config)
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

    if include_completed and operation_id is not None:
        transition_journal = _read_completed_transition_journal_for_operation(
            imported_root=imported_root,
            operation_id=operation_id,
        )
        if transition_journal is not None:
            return _read_snapshot_from_transition_journal(
                config=config,
                journal=transition_journal,
                expected_document_id=document_id,
                expected_operation_id=operation_id,
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


def _validate_completed_operation_against_source(
    *,
    operation: SplitProvenanceRecord,
    source_snapshot: SourceSnapshot,
    source_pdf_name: str,
) -> None:
    _validate_operation_id_format(operation.operation_id)
    normalized_boundaries = _normalize_boundaries(operation.boundaries, source_snapshot.page_count)
    expected_operation_id = _deterministic_operation_id(
        source_snapshot.sidecar.id,
        source_snapshot.source_sha256,
        source_snapshot.page_count,
        normalized_boundaries,
    )
    expected_parts = _build_parts(Path(source_pdf_name).stem, source_snapshot.page_count, normalized_boundaries)
    _validate_operation_record(
        operation,
        expected_operation_id=expected_operation_id,
        expected_source_sha256=source_snapshot.source_sha256,
        expected_page_count=source_snapshot.page_count,
        expected_boundaries=normalized_boundaries,
        expected_parts=expected_parts,
    )


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


def _stage_transition_file(
    stage_path: Path,
    *,
    expected_hash: str,
    source_path: Path,
    label: str,
) -> None:
    if stage_path.exists():
        if stage_path.is_symlink() or not stage_path.is_file():
            raise SplitError(f"Completed transition staged {label} is invalid.")
        _assert_not_hard_link(stage_path, label=f"completed transition staged {label}")
        if _sha256_file_bounded(stage_path, label=f"completed transition staged {label}") != expected_hash:
            raise SplitError(f"Completed transition staged {label} hash mismatch.")
        return
    if source_path.is_symlink() or not source_path.is_file():
        raise SplitError(f"Completed transition source {label} is missing or invalid.")
    _assert_not_hard_link(source_path, label=f"completed transition source {label}")
    try:
        payload = source_path.read_bytes()
    except OSError as exc:
        raise SplitError(f"Could not read completed transition source {label}.") from exc
    _write_bytes_exclusive(stage_path, payload)
    if _sha256_file_bounded(stage_path, label=f"completed transition staged {label}") != expected_hash:
        raise SplitError(f"Completed transition staged {label} hash verification failed.")


def _publish_retained_from_stage(
    stage_path: Path,
    destination_path: Path,
    *,
    expected_hash: str,
    root: Path,
    label: str,
) -> None:
    _assert_not_hard_link(stage_path, label=f"completed transition staged {label}")
    if destination_path.suffix.lower() == ".pdf":
        _validate_direct_destination_path(destination_path, root=root)
    else:
        try:
            assert_within_base(destination_path, root)
        except ValueError as exc:
            raise SplitError(f"{destination_path.name} is outside the expected destination root.") from exc
        if destination_path.parent.resolve() != root.resolve():
            raise SplitError(f"{destination_path.name} must be written directly to {root.name}.")
        if destination_path.suffix.lower() != ".json":
            raise SplitError(f"{destination_path.name} has invalid extension; expected .json.")
        if destination_path.exists() and (destination_path.is_symlink() or not destination_path.is_file()):
            raise SplitError(f"Retained completed source {label} destination is invalid.")
    if destination_path.exists():
        _assert_not_hard_link(destination_path, label=f"retained completed source {label}")
        if _sha256_file_bounded(destination_path, label=f"retained completed {label}") != expected_hash:
            raise SplitError(f"Destination collision for retained completed source {destination_path.name}.")
        return
    try:
        payload = stage_path.read_bytes()
    except OSError as exc:
        raise SplitError(f"Could not read completed transition staged {label}.") from exc
    try:
        _write_bytes_exclusive(destination_path, payload)
    except SplitError:
        if destination_path.exists() and _sha256_file_bounded(
            destination_path, label=f"retained completed {label}"
        ) == expected_hash:
            _assert_not_hard_link(destination_path, label=f"retained completed source {label}")
            return
        raise
    if _sha256_file_bounded(destination_path, label=f"retained completed {label}") != expected_hash:
        raise SplitError(f"Retained completed source {label} hash verification failed.")


def _remove_source_file_if_present(
    source_path: Path,
    *,
    root: Path,
    expected_hash: str,
    label: str,
) -> None:
    if not source_path.exists():
        return
    if source_path.is_symlink() or not source_path.is_file():
        raise SplitError(f"Retained completed source {label} is invalid.")
    _validate_direct_regular_file(
        source_path,
        root=root,
        suffix=".pdf" if source_path.suffix.lower() == ".pdf" else ".json",
    )
    if _sha256_file_bounded(source_path, label=f"completed transition source {label}") != expected_hash:
        raise SplitError(f"Completed transition source {label} hash mismatch.")
    try:
        source_path.unlink()
    except OSError as exc:
        raise SplitError(f"Could not remove completed transition source {label}.") from exc


def _build_completed_summary(
    *,
    sidecar: SidecarDocument,
    operation: SplitProvenanceRecord,
    retained_pdf_name: str,
) -> CompletedSplitSummary:
    return CompletedSplitSummary(
        document_id=sidecar.id,
        retained_pdf=retained_pdf_name,
        source_sha256=sidecar.sha256,
        operation_id=operation.operation_id,
        completed_at_utc=operation.completed_at_utc or operation.created_at_utc,
        outputs=tuple(item.filename for item in operation.outputs),
    )


def _retain_completed_source(
    *,
    config: AktenfuxConfig,
    snapshot: SourceSnapshot,
    operation: SplitProvenanceRecord,
    source_filename: str,
) -> tuple[Path, Path]:
    imported_root = _validate_completed_roots(config, include_inbox=True)
    retained_pdf, retained_sidecar = _completed_destination_paths(
        imported_root=imported_root,
        source_pdf_name=source_filename,
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

    source_pdf = config.split_path / source_filename
    source_sidecar = sidecar_path_for(source_pdf)
    operation_root, transition_root, journal_path = _completed_transition_paths(
        imported_root=imported_root,
        operation_id=operation.operation_id,
    )
    _ensure_directory(imported_root, label="imported")
    _ensure_directory(transition_root, label="completed transition")
    _ensure_directory(operation_root, label="completed transition operation")

    journal = _load_completed_transition_journal(journal_path)
    if journal is None:
        journal = CompletedTransitionJournal(
            operation_id=operation.operation_id,
            document_id=snapshot.sidecar.id,
            source_sha256=snapshot.source_sha256,
            source_pdf=source_pdf.name,
            source_sidecar=source_sidecar.name,
            retained_pdf=retained_pdf.name,
            retained_sidecar=retained_sidecar.name,
            state="planned",
        )
        journal = _write_completed_transition_journal(
            journal_path,
            journal,
            state="planned",
        )
    else:
        if (
            journal.operation_id != operation.operation_id
            or journal.document_id != snapshot.sidecar.id
            or journal.source_sha256 != snapshot.source_sha256
            or journal.source_pdf != source_pdf.name
            or journal.source_sidecar != source_sidecar.name
            or journal.retained_pdf != retained_pdf.name
            or journal.retained_sidecar != retained_sidecar.name
        ):
            raise SplitError("Completed transition journal does not match this operation.")

    stage_pdf = operation_root / "source.pdf"
    stage_sidecar = operation_root / "source.json"

    def sidecar_hash_for_recovery() -> str:
        if source_sidecar.exists():
            return _sha256_file_bounded(source_sidecar, label="split sidecar")
        if retained_sidecar.exists():
            return _sha256_file_bounded(retained_sidecar, label="retained split sidecar")
        if stage_sidecar.exists():
            return _sha256_file_bounded(stage_sidecar, label="completed transition staged sidecar")
        raise SplitError("Completed transition sidecar is missing from source, stage, and retained paths.")

    source_sidecar_hash = sidecar_hash_for_recovery()

    if journal.state == "planned":
        if source_pdf.exists():
            _validate_direct_regular_file(source_pdf, root=config.split_path, suffix=".pdf")
            if _sha256_file_bounded(source_pdf, label="split source") != snapshot.source_sha256:
                raise SplitError("Source PDF hash changed before completed-source transition.")
        if source_sidecar.exists():
            _verify_completed_sidecar_identity(
                source_sidecar,
                document_id=snapshot.sidecar.id,
                source_sha256=snapshot.source_sha256,
                operation_id=operation.operation_id,
            )
            source_sidecar_hash = sidecar_hash_for_recovery()
        _stage_transition_file(
            stage_pdf,
            expected_hash=snapshot.source_sha256,
            source_path=source_pdf if source_pdf.exists() else retained_pdf,
            label="PDF",
        )
        journal = _write_completed_transition_journal(journal_path, journal, state="pdf_staged")
    if journal.state == "pdf_staged":
        source_sidecar_hash = sidecar_hash_for_recovery()
        _stage_transition_file(
            stage_sidecar,
            expected_hash=source_sidecar_hash,
            source_path=source_sidecar if source_sidecar.exists() else retained_sidecar,
            label="sidecar",
        )
        journal = _write_completed_transition_journal(journal_path, journal, state="sidecar_staged")
    if journal.state == "sidecar_staged":
        _publish_retained_from_stage(
            stage_pdf,
            retained_pdf,
            expected_hash=snapshot.source_sha256,
            root=imported_root,
            label="PDF",
        )
        journal = _write_completed_transition_journal(
            journal_path,
            journal,
            state="retained_pdf_published",
        )
    if journal.state == "retained_pdf_published":
        _publish_retained_from_stage(
            stage_sidecar,
            retained_sidecar,
            expected_hash=source_sidecar_hash,
            root=imported_root,
            label="sidecar",
        )
        journal = _write_completed_transition_journal(
            journal_path,
            journal,
            state="retained_sidecar_published",
        )
    if journal.state == "retained_sidecar_published":
        _remove_source_file_if_present(
            source_pdf,
            root=config.split_path,
            expected_hash=snapshot.source_sha256,
            label="PDF",
        )
        journal = _write_completed_transition_journal(
            journal_path,
            journal,
            state="source_pdf_removed",
        )
    if journal.state == "source_pdf_removed":
        _remove_source_file_if_present(
            source_sidecar,
            root=config.split_path,
            expected_hash=source_sidecar_hash,
            label="sidecar",
        )
        journal = _write_completed_transition_journal(
            journal_path,
            journal,
            state="source_sidecar_removed",
        )
    if journal.state == "source_sidecar_removed":
        journal = _write_completed_transition_journal(
            journal_path,
            journal,
            state="finalized",
        )
    if journal.state != "finalized":
        raise SplitError("Completed transition did not reach finalized state.")

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
    _cleanup_completed_transition_artifacts(
        operation_root=operation_root,
        transition_root=transition_root,
    )
    return retained_pdf, retained_sidecar


def list_completed_split_documents(config: AktenfuxConfig) -> CompletedSplitListResult:
    """Return retained completed split sources from _Imported with bounded issues."""
    imported_root = _validate_completed_roots(config, include_inbox=False)
    if not imported_root.exists():
        return CompletedSplitListResult(entries=tuple(), issues=tuple())
    entries: list[CompletedSplitSummary] = []
    issues: list[CompletedSplitListIssue] = []
    seen_ids: set[str] = set()
    pdf_paths = sorted(imported_root.glob("*.pdf"))
    json_paths = sorted(imported_root.glob("*.json"))
    pdf_names = {path.name for path in pdf_paths}
    json_names = {path.name for path in json_paths}
    for json_name in sorted(json_names - {Path(name).with_suffix(".json").name for name in pdf_names}):
        issues.append(
            CompletedSplitListIssue(
                retained_pdf=Path(json_name).with_suffix(".pdf").name,
                reason=f"retained completed source pair is incomplete ({json_name} has no matching PDF).",
            )
        )
    for pdf_path in pdf_paths:
        try:
            _validate_direct_regular_file(pdf_path, root=imported_root, suffix=".pdf")
            sidecar_path = sidecar_path_for(pdf_path)
            _validate_direct_regular_file(sidecar_path, root=imported_root, suffix=".json")
            snapshot = _read_validated_snapshot(
                pdf_path,
                sidecar_path,
                _read_sidecar_validated(sidecar_path),
                lifecycle_root=imported_root,
                lifecycle_label="imported",
            )
            sidecar = snapshot.sidecar
            operation = _completed_operation_for_document(sidecar)
            if operation is None:
                continue
            source_pdf_name = _derive_original_source_pdf_name_from_retained(
                pdf_path.name,
                operation.operation_id,
            )
            _validate_completed_operation_against_source(
                operation=operation,
                source_snapshot=snapshot,
                source_pdf_name=source_pdf_name,
            )
            if sidecar.id in seen_ids:
                raise SplitError("Duplicate document id found in retained completed sources.")
            seen_ids.add(sidecar.id)
            entries.append(
                _build_completed_summary(
                    sidecar=sidecar,
                    operation=operation,
                    retained_pdf_name=pdf_path.name,
                )
            )
        except SplitError as exc:
            issues.append(
                CompletedSplitListIssue(
                    retained_pdf=pdf_path.name,
                    reason=str(exc),
                )
            )
    return CompletedSplitListResult(entries=tuple(entries), issues=tuple(issues))


def _validate_completed_outputs(
    *,
    config: AktenfuxConfig,
    operation: SplitProvenanceRecord,
) -> None:
    for output in operation.outputs:
        if output.sha256 is None:
            raise SplitError(f"missing output hash for {output.filename}")
        final_path = config.inbox_path / output.filename
        _validate_direct_destination_path(final_path, root=config.inbox_path)
        if not final_path.exists():
            raise SplitError(f"missing output {output.filename}")
        if _sha256_file_bounded(final_path, label="completed split output") != output.sha256:
            raise SplitError(f"output hash mismatch for {output.filename}")


def _state_rank(state: str) -> int:
    try:
        return _COMPLETED_TRANSITION_STATES.index(state)
    except ValueError as exc:
        raise SplitError("Completed transition journal state is invalid.") from exc


def _sidecar_semantic_payload_without_current_path(sidecar: SidecarDocument) -> dict[str, Any]:
    payload = sidecar.model_dump(mode="json")
    payload.pop("current_path", None)
    return payload


def _validate_allowed_finalized_sidecar_current_path_drift(
    *,
    staged_sidecar: SidecarDocument,
    snapshot_sidecar: SidecarDocument,
    expected_split_path: str,
    expected_imported_path: str,
) -> None:
    if _sidecar_semantic_payload_without_current_path(staged_sidecar) != _sidecar_semantic_payload_without_current_path(
        snapshot_sidecar
    ):
        raise SplitError("sidecar staging copy semantic mismatch")
    allowed_paths = {expected_split_path, expected_imported_path}
    if staged_sidecar.current_path not in allowed_paths or snapshot_sidecar.current_path not in allowed_paths:
        raise SplitError("sidecar staging copy current_path is invalid")
    if staged_sidecar.current_path == snapshot_sidecar.current_path:
        return
    if staged_sidecar.current_path != expected_split_path or snapshot_sidecar.current_path != expected_imported_path:
        raise SplitError("sidecar staging copy current_path drift is invalid")


def _preflight_completed_transition_from_journal(
    *,
    config: AktenfuxConfig,
    journal: CompletedTransitionJournal,
) -> tuple[SourceSnapshot, SplitProvenanceRecord, CompletedSplitSummary]:
    snapshot = _read_snapshot_from_transition_journal(
        config=config,
        journal=journal,
        expected_document_id=journal.document_id,
        expected_operation_id=journal.operation_id,
    )
    sidecar = snapshot.sidecar
    operation = _completed_operation_for_document(sidecar, operation_id=journal.operation_id)
    if operation is None:
        raise SplitError("completed operation missing in sidecar")
    _validate_completed_operation_against_source(
        operation=operation,
        source_snapshot=snapshot,
        source_pdf_name=journal.source_pdf,
    )
    _validate_completed_outputs(config=config, operation=operation)
    imported_root = _validate_imported_root(config)
    retained_pdf, retained_sidecar = _completed_destination_paths(
        imported_root=imported_root,
        source_pdf_name=journal.source_pdf,
        operation_id=journal.operation_id,
    )
    source_pdf = config.split_path / journal.source_pdf
    source_sidecar = config.split_path / journal.source_sidecar
    stage_root, _, _journal_path = _completed_transition_paths(
        imported_root=imported_root,
        operation_id=journal.operation_id,
    )
    stage_pdf = stage_root / "source.pdf"
    stage_sidecar = stage_root / "source.json"

    source_sidecar_hash = _sha256_file_bounded(snapshot.sidecar_path, label="completed transition sidecar")
    stage_pdf_present = False
    if stage_pdf.exists():
        _validate_direct_regular_file(stage_pdf, root=stage_root, suffix=".pdf")
        if _sha256_file_bounded(stage_pdf, label="completed transition staged PDF") != journal.source_sha256:
            raise SplitError("pdf staging copy hash mismatch")
        stage_pdf_present = True
    stage_sidecar_present = False
    if stage_sidecar.exists():
        _validate_direct_regular_file(stage_sidecar, root=stage_root, suffix=".json")
        stage_sidecar_hash = _sha256_file_bounded(stage_sidecar, label="completed transition staged sidecar")
        if stage_sidecar_hash != source_sidecar_hash:
            if journal.state != "finalized":
                raise SplitError("sidecar staging copy hash mismatch")
            staged_sidecar = _read_sidecar_validated(stage_sidecar)
            _verify_completed_sidecar_identity(
                stage_sidecar,
                document_id=journal.document_id,
                source_sha256=journal.source_sha256,
                operation_id=journal.operation_id,
            )
            expected_split_path = str((config.split_path / journal.source_pdf).resolve())
            expected_imported_path = str((config.imported_path / journal.retained_pdf).resolve())
            _validate_allowed_finalized_sidecar_current_path_drift(
                staged_sidecar=staged_sidecar,
                snapshot_sidecar=snapshot.sidecar,
                expected_split_path=expected_split_path,
                expected_imported_path=expected_imported_path,
            )
        stage_sidecar_present = True
    retained_pdf_present = False
    if retained_pdf.exists():
        _validate_direct_regular_file(retained_pdf, root=imported_root, suffix=".pdf")
        if _sha256_file_bounded(retained_pdf, label="retained completed PDF") != journal.source_sha256:
            raise SplitError("retained PDF hash mismatch")
        retained_pdf_present = True
    retained_sidecar_present = False
    if retained_sidecar.exists():
        _validate_direct_regular_file(retained_sidecar, root=imported_root, suffix=".json")
        if _sha256_file_bounded(retained_sidecar, label="retained completed sidecar") != source_sidecar_hash:
            raise SplitError("retained sidecar hash mismatch")
        _verify_completed_sidecar_identity(
            retained_sidecar,
            document_id=journal.document_id,
            source_sha256=journal.source_sha256,
            operation_id=journal.operation_id,
        )
        retained_sidecar_present = True

    rank = _state_rank(journal.state)
    if rank >= _state_rank("pdf_staged") and not stage_pdf_present:
        raise SplitError("pdf staging copy missing")
    if rank >= _state_rank("sidecar_staged") and not stage_sidecar_present:
        raise SplitError("sidecar staging copy missing")
    if rank >= _state_rank("retained_pdf_published") and not retained_pdf_present:
        raise SplitError("retained PDF missing")
    if rank >= _state_rank("retained_sidecar_published") and not retained_sidecar_present:
        raise SplitError("retained sidecar missing")
    if rank >= _state_rank("source_pdf_removed") and source_pdf.exists():
        raise SplitError("source PDF cleanup incomplete")
    if rank >= _state_rank("source_sidecar_removed") and source_sidecar.exists():
        raise SplitError("source sidecar cleanup incomplete")

    summary = _build_completed_summary(
        sidecar=sidecar,
        operation=operation,
        retained_pdf_name=retained_pdf.name,
    )
    return snapshot, operation, summary


def reconcile_completed_split_sources(
    config: AktenfuxConfig,
    *,
    apply_changes: bool,
) -> CompletedSplitReconcileResult:
    """Dry-run-first reconciliation for completed split sources still in _Split."""
    imported_root = _validate_completed_roots(config, include_inbox=True)
    eligible: list[CompletedSplitSummary] = []
    transitioned: list[CompletedSplitSummary] = []
    issues: list[CompletedSplitReconcileIssue] = []
    seen_operations: set[str] = set()

    journals: list[CompletedTransitionJournal] = []
    try:
        journals = _list_completed_transition_journals(imported_root)
    except SplitError as exc:
        issues.append(
            CompletedSplitReconcileIssue(
                document_id="(unknown)",
                source_pdf="(transition-journal)",
                reason=str(exc),
            )
        )

    if not config.split_path.exists():
        if journals:
            for journal in journals:
                try:
                    snapshot, operation, summary = _preflight_completed_transition_from_journal(
                        config=config,
                        journal=journal,
                    )
                    seen_operations.add(journal.operation_id)
                    eligible.append(summary)
                    if apply_changes:
                        _retain_completed_source(
                            config=config,
                            snapshot=snapshot,
                            operation=operation,
                            source_filename=journal.source_pdf,
                        )
                        transitioned.append(summary)
                except SplitError as exc:
                    issues.append(
                        CompletedSplitReconcileIssue(
                            document_id=journal.document_id,
                            source_pdf=journal.source_pdf,
                            reason=str(exc),
                        )
                    )
        return CompletedSplitReconcileResult(
            dry_run=not apply_changes,
            eligible=tuple(eligible),
            transitioned=tuple(transitioned),
            issues=tuple(issues),
        )

    split_pdf_paths = sorted(config.split_path.glob("*.pdf"))
    split_json_names = {path.name for path in config.split_path.glob("*.json")}
    for json_name in sorted(split_json_names - {path.with_suffix(".json").name for path in split_pdf_paths}):
        issues.append(
            CompletedSplitReconcileIssue(
                document_id="(unknown)",
                source_pdf=Path(json_name).with_suffix(".pdf").name,
                reason=f"split source pair is incomplete ({json_name} has no matching PDF).",
            )
        )

    for journal in journals:
        try:
            snapshot, operation, summary = _preflight_completed_transition_from_journal(
                config=config,
                journal=journal,
            )
            if summary.operation_id in seen_operations:
                raise SplitError("duplicate completed transition journal operation")
            seen_operations.add(summary.operation_id)
            eligible.append(summary)
            if apply_changes:
                _retain_completed_source(
                    config=config,
                    snapshot=snapshot,
                    operation=operation,
                    source_filename=journal.source_pdf,
                )
                transitioned.append(summary)
        except SplitError as exc:
            issues.append(
                CompletedSplitReconcileIssue(
                    document_id=journal.document_id,
                    source_pdf=journal.source_pdf,
                    reason=str(exc),
                )
            )

    for pdf_path in split_pdf_paths:
        document_id = "(unknown)"
        try:
            if not pdf_path.exists():
                continue
            _validate_direct_regular_file(pdf_path, root=config.split_path, suffix=".pdf")
            sidecar_path = sidecar_path_for(pdf_path)
            _validate_direct_regular_file(sidecar_path, root=config.split_path, suffix=".json")
            sidecar = _read_sidecar_validated(sidecar_path)
            document_id = sidecar.id
            operation = _completed_operation_for_document(sidecar)
            if operation is None:
                continue
            if operation.operation_id in seen_operations:
                continue
            if len([op for op in sidecar.split_operations if op.state == "completed"]) != 1:
                raise SplitError("multiple completed split operations")
            snapshot = _read_validated_snapshot(
                pdf_path,
                sidecar_path,
                sidecar,
                lifecycle_root=config.split_path,
                lifecycle_label="split",
            )
            if snapshot.source_sha256 != sidecar.sha256:
                raise SplitError("sidecar/source hash mismatch")
            _validate_completed_operation_against_source(
                operation=operation,
                source_snapshot=snapshot,
                source_pdf_name=pdf_path.name,
            )
            _validate_completed_outputs(config=config, operation=operation)
            retained_pdf, retained_sidecar = _completed_destination_paths(
                imported_root=imported_root,
                source_pdf_name=pdf_path.name,
                operation_id=operation.operation_id,
            )
            retained_pdf_exists = retained_pdf.exists()
            retained_sidecar_exists = retained_sidecar.exists()
            if retained_pdf_exists != retained_sidecar_exists:
                raise SplitError("retained destination pair is partial and requires recovery via journal.")
            if retained_pdf_exists:
                _validate_direct_regular_file(retained_pdf, root=imported_root, suffix=".pdf")
                _validate_direct_regular_file(retained_sidecar, root=imported_root, suffix=".json")
                if _sha256_file_bounded(retained_pdf, label="retained split source") != sidecar.sha256:
                    raise SplitError("retained destination PDF collision")
                _verify_completed_sidecar_identity(
                    retained_sidecar,
                    document_id=sidecar.id,
                    source_sha256=sidecar.sha256,
                    operation_id=operation.operation_id,
                )
            summary = _build_completed_summary(
                sidecar=sidecar,
                operation=operation,
                retained_pdf_name=retained_pdf.name,
            )
            if summary.operation_id in seen_operations:
                raise SplitError("duplicate completed operation candidate")
            seen_operations.add(summary.operation_id)
            eligible.append(summary)
            if apply_changes:
                _retain_completed_source(
                    config=config,
                    snapshot=snapshot,
                    operation=operation,
                    source_filename=pdf_path.name,
                )
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
        _retain_completed_source(
            config=config,
            snapshot=snapshot,
            operation=operation,
            source_filename=plan.source_filename,
        )
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
    _retain_completed_source(
        config=config,
        snapshot=snapshot,
        operation=operation,
        source_filename=plan.source_filename,
    )

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
