"""Recoverable document split planning and execution services."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Iterable

from pypdf import PdfReader, PdfWriter

from aktenfux.config import AktenfuxConfig
from aktenfux.review import find_document_by_id
from aktenfux.schema import (
    SidecarDocument,
    SplitOutputRecord,
    SplitPartPlan,
    SplitPartResult,
    SplitPlan,
    SplitProvenanceRecord,
    SplitResult,
)
from aktenfux.storage import assert_within_base, sha256_file, sidecar_path_for

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _deterministic_operation_id(
    document_id: str, source_sha256: str, page_count: int, boundaries: list[int]
) -> str:
    payload = f"{document_id}|{source_sha256}|{page_count}|{','.join(str(v) for v in boundaries)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _normalize_boundaries(boundaries: Iterable[int], page_count: int) -> list[int]:
    normalized = sorted(boundaries)
    if not normalized:
        raise ValueError("At least one --before boundary is required.")
    if len(set(normalized)) != len(normalized):
        raise ValueError("Duplicate boundaries are not allowed.")
    for boundary in normalized:
        if boundary < 2 or boundary > page_count:
            raise ValueError(
                f"Boundary {boundary} is out of range. Expected values in 2..{page_count}."
            )
    return normalized


def _build_parts(source_stem: str, page_count: int, boundaries: list[int]) -> list[SplitPartPlan]:
    starts = [1, *boundaries]
    ends = [*(boundary - 1 for boundary in boundaries), page_count]
    parts: list[SplitPartPlan] = []
    for idx, (start, end) in enumerate(zip(starts, ends), start=1):
        if start > end:
            raise ValueError("Boundaries produce an empty split part.")
        parts.append(
            SplitPartPlan(
                part_number=idx,
                start_page=start,
                end_page=end,
                destination_filename=f"{source_stem}--part-{idx:02d}.pdf",
            )
        )
    return parts


def _validate_direct_regular_file(path: Path, *, root: Path, suffix: str) -> None:
    try:
        assert_within_base(path, root)
    except ValueError as exc:
        raise ValueError(f"{path.name} is outside the expected lifecycle root.") from exc
    if path.parent.resolve() != root.resolve():
        raise ValueError(f"{path.name} must be located directly in {root.name}.")
    if path.suffix.lower() != suffix:
        raise ValueError(f"{path.name} has invalid extension; expected {suffix}.")
    if path.is_symlink():
        raise ValueError(f"{path.name} must not be a symbolic link.")
    if not path.is_file():
        raise ValueError(f"{path.name} must be a regular file.")


def _validate_direct_destination_path(path: Path, *, root: Path) -> None:
    try:
        assert_within_base(path, root)
    except ValueError as exc:
        raise ValueError(f"{path.name} is outside the expected destination root.") from exc
    if path.parent.resolve() != root.resolve():
        raise ValueError(f"{path.name} must be written directly to {root.name}.")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"{path.name} has invalid extension; expected .pdf.")
    if path.exists():
        if path.is_symlink():
            raise ValueError(f"{path.name} must not be a symbolic link.")
        if not path.is_file():
            raise ValueError(f"{path.name} must be a regular file.")


def _find_split_source(document_id: str, config: AktenfuxConfig) -> tuple[Path, SidecarDocument]:
    result = find_document_by_id(config.split_path, document_id)
    if result is None:
        raise FileNotFoundError(f"Document '{document_id}' not found in _Split.")

    pdf_path, sidecar = result
    _validate_direct_regular_file(pdf_path, root=config.split_path, suffix=".pdf")

    sidecar_path = sidecar_path_for(pdf_path)
    _validate_direct_regular_file(sidecar_path, root=config.split_path, suffix=".json")
    return pdf_path, sidecar


def _read_page_count(pdf_path: Path) -> int:
    try:
        reader = PdfReader(str(pdf_path))
        return len(reader.pages)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Failed to parse PDF '{pdf_path.name}' for page count.") from exc


def _write_sidecar_atomic(sidecar: SidecarDocument, pdf_path: Path) -> None:
    dest = sidecar_path_for(pdf_path)
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(sidecar.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(dest)


def _part_pdf_bytes(reader: PdfReader, *, start_page: int, end_page: int) -> bytes:
    writer = PdfWriter()
    for page_index in range(start_page - 1, end_page):
        writer.add_page(reader.pages[page_index])
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def plan_split(document_id: str, boundaries: list[int], config: AktenfuxConfig) -> SplitPlan:
    """Return a read-only split plan for a document staged in _Split."""
    pdf_path, sidecar = _find_split_source(document_id, config)
    page_count = _read_page_count(pdf_path)
    if page_count < 2:
        raise ValueError("Single-page PDFs cannot be split.")

    normalized_boundaries = _normalize_boundaries(boundaries, page_count)
    parts = _build_parts(pdf_path.stem, page_count, normalized_boundaries)
    source_sha256 = sha256_file(pdf_path)
    operation_id = _deterministic_operation_id(
        sidecar.id, source_sha256, page_count, normalized_boundaries
    )

    return SplitPlan(
        operation_id=operation_id,
        document_id=sidecar.id,
        source_filename=pdf_path.name,
        source_sha256=source_sha256,
        source_page_count=page_count,
        boundaries=normalized_boundaries,
        parts=parts,
    )


def execute_split(plan: SplitPlan, config: AktenfuxConfig) -> SplitResult:
    """Execute a split plan with recoverable, idempotent semantics."""
    pdf_path, sidecar = _find_split_source(plan.document_id, config)
    page_count = _read_page_count(pdf_path)
    source_sha256 = sha256_file(pdf_path)

    if pdf_path.name != plan.source_filename:
        raise RuntimeError("Source filename changed after planning.")
    if page_count != plan.source_page_count or source_sha256 != plan.source_sha256:
        raise RuntimeError("Source document changed after planning.")

    normalized_boundaries = _normalize_boundaries(plan.boundaries, page_count)
    planned_parts = _build_parts(pdf_path.stem, page_count, normalized_boundaries)
    if planned_parts != plan.parts:
        raise RuntimeError("Split plan no longer matches deterministic output naming.")

    operation = next(
        (candidate for candidate in sidecar.split_operations if candidate.operation_id == plan.operation_id),
        None,
    )
    if operation is None:
        operation = SplitProvenanceRecord(
            operation_id=plan.operation_id,
            source_sha256=plan.source_sha256,
            source_page_count=plan.source_page_count,
            boundaries=plan.boundaries,
            outputs=[
                SplitOutputRecord(
                    part_number=part.part_number,
                    start_page=part.start_page,
                    end_page=part.end_page,
                    filename=part.destination_filename,
                )
                for part in plan.parts
            ],
            state="in_progress",
            created_at_utc=_utc_now(),
        )
        sidecar.split_operations.append(operation)
        _write_sidecar_atomic(sidecar, pdf_path)

    if operation.source_sha256 != plan.source_sha256 or operation.boundaries != plan.boundaries:
        raise RuntimeError("Existing split operation metadata does not match this plan.")

    reader = PdfReader(str(pdf_path))
    config.inbox_path.mkdir(parents=True, exist_ok=True)

    for output in operation.outputs:
        dest = config.inbox_path / output.filename
        _validate_direct_destination_path(dest, root=config.inbox_path)
        if dest.exists():
            if output.sha256 is None:
                raise FileExistsError(
                    f"Destination collision for {output.filename}; no matching split retry record exists."
                )
            existing_hash = sha256_file(dest)
            if existing_hash != output.sha256:
                raise FileExistsError(
                    f"Destination collision for {output.filename}; existing content does not match retry hash."
                )
            continue

        part_bytes = _part_pdf_bytes(reader, start_page=output.start_page, end_page=output.end_page)
        part_hash = hashlib.sha256(part_bytes).hexdigest()
        with dest.open("xb") as fh:
            fh.write(part_bytes)
        written_hash = sha256_file(dest)
        if written_hash != part_hash:
            raise RuntimeError(f"Hash verification failed for {output.filename}.")
        output.sha256 = part_hash
        _write_sidecar_atomic(sidecar, pdf_path)

    operation.state = "completed"
    operation.completed_at_utc = _utc_now()
    _write_sidecar_atomic(sidecar, pdf_path)

    return SplitResult(
        operation_id=plan.operation_id,
        document_id=plan.document_id,
        source_filename=plan.source_filename,
        source_sha256=plan.source_sha256,
        source_page_count=plan.source_page_count,
        boundaries=plan.boundaries,
        outputs=[
            SplitPartResult(
                part_number=output.part_number,
                start_page=output.start_page,
                end_page=output.end_page,
                filename=output.filename,
                sha256=output.sha256 or "",
            )
            for output in operation.outputs
        ],
        completed_at_utc=operation.completed_at_utc or _utc_now(),
    )
