"""Recoverable document split planning and execution services."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

from pypdf import PdfReader, PdfWriter

from aktenfux.config import AktenfuxConfig
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


class SplitError(RuntimeError):
    """Bounded, audit-safe application error for split workflows."""


@dataclass(frozen=True)
class SourceSnapshot:
    pdf_path: Path
    pdf_name: str
    sidecar_path: Path
    sidecar: SidecarDocument
    source_bytes: bytes
    source_sha256: str
    page_count: int


@dataclass(frozen=True)
class PublishJournal:
    state: str
    outputs: tuple[str, ...]
    published: frozenset[str]


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


def _find_source_by_document_id(split_root: Path, document_id: str) -> tuple[Path, Path, SidecarDocument]:
    if not split_root.exists():
        raise SplitError("_Split directory is missing.")

    exact: tuple[Path, Path, SidecarDocument] | None = None
    prefix_matches: list[tuple[Path, Path, SidecarDocument]] = []

    for pdf_path in sorted(split_root.glob("*.pdf")):
        try:
            _validate_direct_regular_file(pdf_path, root=split_root, suffix=".pdf")
            sidecar_path = sidecar_path_for(pdf_path)
            _validate_direct_regular_file(sidecar_path, root=split_root, suffix=".json")
            sidecar = _read_sidecar_validated(sidecar_path)
        except SplitError as exc:
            logger.warning("Skipping invalid _Split candidate '%s': %s", pdf_path.name, exc)
            continue

        if sidecar.id == document_id:
            exact = (pdf_path, sidecar_path, sidecar)
            break
        if sidecar.id.startswith(document_id):
            prefix_matches.append((pdf_path, sidecar_path, sidecar))

    if exact is not None:
        return exact
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    if len(prefix_matches) > 1:
        raise SplitError(f"Document id prefix '{document_id}' is ambiguous in _Split.")
    raise SplitError(f"Document '{document_id}' not found in _Split.")


def _read_snapshot(config: AktenfuxConfig, document_id: str) -> SourceSnapshot:
    pdf_path, sidecar_path, sidecar = _find_source_by_document_id(config.split_path, document_id)
    try:
        with pdf_path.open("rb") as fh:
            source_bytes = fh.read()
    except OSError as exc:
        raise SplitError(f"Could not read source PDF '{pdf_path.name}'.") from exc

    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    try:
        page_count = len(PdfReader(BytesIO(source_bytes)).pages)
    except Exception as exc:  # noqa: BLE001
        raise SplitError(f"Failed to parse source PDF '{pdf_path.name}'.") from exc

    return SourceSnapshot(
        pdf_path=pdf_path,
        pdf_name=pdf_path.name,
        sidecar_path=sidecar_path,
        sidecar=sidecar,
        source_bytes=source_bytes,
        source_sha256=source_sha256,
        page_count=page_count,
    )


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


def _write_bytes_exclusive_atomic(path: Path, data: bytes) -> None:
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
    expected_parts = _build_parts(snapshot.pdf_path.stem, snapshot.page_count, normalized_boundaries)
    expected_operation_id = _deterministic_operation_id(
        snapshot.sidecar.id, snapshot.source_sha256, snapshot.page_count, normalized_boundaries
    )

    if plan.document_id != snapshot.sidecar.id:
        raise SplitError("Split plan document identity mismatch.")
    if plan.source_filename != snapshot.pdf_name:
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
    _write_bytes_exclusive_atomic(staging_path, part_bytes)
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
    _write_bytes_exclusive_atomic(final_path, data)
    if _sha256_file_bounded(final_path, label="split output") != expected_hash:
        raise SplitError(f"Split output hash verification failed for '{final_path.name}'.")


def plan_split(document_id: str, boundaries: list[int], config: AktenfuxConfig) -> SplitPlan:
    """Return a read-only split plan for a document staged in _Split."""
    snapshot = _read_snapshot(config, document_id)
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
    snapshot = _read_snapshot(config, plan.document_id)
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
        return SplitResult(
            operation_id=operation.operation_id,
            document_id=plan.document_id,
            source_filename=snapshot.pdf_name,
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
        source_filename=snapshot.pdf_name,
        source_sha256=snapshot.source_sha256,
        source_page_count=snapshot.page_count,
        boundaries=plan.boundaries,
        outputs=outputs,
        completed_at_utc=operation.completed_at_utc or _utc_now(),
    )
