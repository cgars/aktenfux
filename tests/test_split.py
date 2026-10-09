"""Tests for recoverable split planning/execution services."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from pypdf import PdfReader
from pypdf import PdfWriter

import aktenfux.split as split_module
from aktenfux.schema import SplitOutputRecord, SplitProvenanceRecord
from aktenfux.split import SplitError, execute_split, plan_split
from aktenfux.storage import read_sidecar, sha256_file
from tests.split_helpers import make_config, pdf_bytes, write_split_doc


def test_plan_split_returns_deterministic_ranges_and_names(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docsplit000000001", pages=10)

    plan = plan_split("docsplit000000001", [8, 4], cfg)

    assert plan.boundaries == [4, 8]
    assert [(part.start_page, part.end_page) for part in plan.parts] == [(1, 3), (4, 7), (8, 10)]
    assert [part.destination_filename for part in plan.parts] == [
        "bundle--part-01.pdf",
        "bundle--part-02.pdf",
        "bundle--part-03.pdf",
    ]


def test_plan_split_rejects_duplicate_boundaries(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docsplit000000002", pages=6)

    with pytest.raises(SplitError, match="Duplicate boundaries"):
        plan_split("docsplit000000002", [3, 3], cfg)


def test_plan_split_rejects_single_page_pdf(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "single.pdf", "docsplit000000003", pages=1)

    with pytest.raises(SplitError, match="Single-page PDFs"):
        plan_split("docsplit000000003", [2], cfg)


def test_execute_split_writes_parts_and_records_provenance(tmp_path):
    cfg = make_config(tmp_path)
    source = write_split_doc(cfg.split_path, "source.pdf", "docsplit000000004", pages=5)
    source_hash_before = sha256_file(source)
    plan = plan_split("docsplit000000004", [3], cfg)

    result = execute_split(plan, cfg)

    output_paths = [cfg.inbox_path / output.filename for output in result.outputs]
    assert all(path.exists() for path in output_paths)
    assert [len(PdfReader(str(path)).pages) for path in output_paths] == [2, 3]
    assert sha256_file(source) == source_hash_before

    sidecar = read_sidecar(source)
    assert sidecar is not None
    assert len(sidecar.split_operations) == 1
    operation = sidecar.split_operations[0]
    assert operation.operation_id == plan.operation_id
    assert operation.state == "completed"
    assert operation.completed_at_utc is not None
    assert all(output.sha256 is not None for output in operation.outputs)


def test_execute_split_rejects_tampered_sidecar_output_ranges(tmp_path):
    cfg = make_config(tmp_path)
    source = write_split_doc(cfg.split_path, "tampered.pdf", "docsplit000000005", pages=4)
    plan = plan_split("docsplit000000005", [3], cfg)

    sidecar = read_sidecar(source)
    assert sidecar is not None
    sidecar.split_operations = [
        SplitProvenanceRecord(
            operation_id=plan.operation_id,
            source_sha256=plan.source_sha256,
            source_page_count=plan.source_page_count,
            boundaries=plan.boundaries,
            outputs=[
                SplitOutputRecord(
                    part_number=1,
                    start_page=1,
                    end_page=1,
                    filename="tampered--part-01.pdf",
                ),
                SplitOutputRecord(
                    part_number=2,
                    start_page=1,
                    end_page=1,
                    filename="tampered--part-02.pdf",
                ),
            ],
            state="in_progress",
            created_at_utc="2026-10-09T00:00:00Z",
        )
    ]
    source.with_suffix(".json").write_text(sidecar.model_dump_json(indent=2), encoding="utf-8")

    with pytest.raises(SplitError, match="page ranges mismatch"):
        execute_split(plan, cfg)


def test_execute_split_rejects_arbitrary_operation_id(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "opid.pdf", "docsplit000000006", pages=4)
    plan = plan_split("docsplit000000006", [3], cfg)
    forged = plan.model_copy(update={"operation_id": "f" * 32})

    with pytest.raises(SplitError, match="operation ID is invalid"):
        execute_split(forged, cfg)


def test_execute_split_detects_same_page_count_source_swap(tmp_path):
    cfg = make_config(tmp_path)
    source = write_split_doc(cfg.split_path, "swap.pdf", "docsplit000000007", pages=4)
    plan = plan_split("docsplit000000007", [3], cfg)
    writer = PdfWriter()
    for _ in range(4):
        writer.add_blank_page(width=90, height=90)
    swapped = Path(tmp_path) / "swapped.pdf"
    writer.write(str(swapped))
    source.write_bytes(swapped.read_bytes())

    with pytest.raises(SplitError, match="source identity mismatch"):
        execute_split(plan, cfg)


def test_execute_split_does_not_republish_completed_outputs(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "complete.pdf", "docsplit000000008", pages=5)
    plan = plan_split("docsplit000000008", [3], cfg)
    result = execute_split(plan, cfg)

    first_output = cfg.inbox_path / result.outputs[0].filename
    first_output.unlink()

    with pytest.raises(SplitError, match="refusing to republish"):
        execute_split(plan, cfg)


def test_execute_split_rejects_untracked_destination_collision(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "collision.pdf", "docsplit000000009", pages=6)
    plan = plan_split("docsplit000000009", [4], cfg)
    cfg.inbox_path.mkdir(parents=True, exist_ok=True)
    (cfg.inbox_path / "collision--part-01.pdf").write_bytes(pdf_bytes(1))

    with pytest.raises(SplitError, match="Destination collision"):
        execute_split(plan, cfg)


def test_plan_split_rejects_symlink_source(tmp_path):
    cfg = make_config(tmp_path)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(pdf_bytes(3))
    symlink_pdf = cfg.split_path / "linked.pdf"
    cfg.split_path.mkdir(parents=True, exist_ok=True)
    try:
        symlink_pdf.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("Symlinks are not available on this platform.")

    sidecar_payload = {
        "id": "docsplit000000010",
        "original_path": str(symlink_pdf),
        "current_path": str(symlink_pdf),
        "sha256": "a" * 64,
    }
    symlink_pdf.with_suffix(".json").write_text(json.dumps(sidecar_payload), encoding="utf-8")

    with pytest.raises(SplitError, match="not found in _Split"):
        plan_split("docsplit000000010", [2], cfg)


def test_plan_split_skips_unrelated_invalid_split_candidate(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "good.pdf", "docsplit000000010a", pages=4)
    outside = tmp_path / "outside-invalid.pdf"
    outside.write_bytes(pdf_bytes(2))
    invalid_pdf = cfg.split_path / "invalid-link.pdf"
    try:
        invalid_pdf.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("Symlinks are not available on this platform.")
    invalid_pdf.with_suffix(".json").write_text(
        json.dumps(
            {
                "id": "docsplit000000010b",
                "original_path": str(invalid_pdf),
                "current_path": str(invalid_pdf),
                "sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )

    plan = plan_split("docsplit000000010a", [3], cfg)
    assert plan.source_filename == "good.pdf"


def test_execute_split_recovers_after_hash_provenance_write_failure(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "recover-hash.pdf", "docsplit000000011", pages=5)
    plan = plan_split("docsplit000000011", [3], cfg)

    original_write_sidecar = split_module._write_sidecar_atomic
    calls = {"count": 0}

    def flaky_write_sidecar(sidecar, sidecar_path):
        calls["count"] += 1
        # 1: initial in_progress create, 2: first output hash persistence -> fail.
        if calls["count"] == 2:
            raise SplitError("injected sidecar write failure")
        return original_write_sidecar(sidecar, sidecar_path)

    monkeypatch.setattr(split_module, "_write_sidecar_atomic", flaky_write_sidecar)
    with pytest.raises(SplitError, match="injected sidecar write failure"):
        execute_split(plan, cfg)

    assert not (cfg.inbox_path / "recover-hash--part-01.pdf").exists()

    monkeypatch.setattr(split_module, "_write_sidecar_atomic", original_write_sidecar)
    result = execute_split(plan, cfg)
    assert (cfg.inbox_path / result.outputs[0].filename).exists()


def test_execute_split_recovers_after_output_write_failure(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "recover-write.pdf", "docsplit000000012", pages=5)
    plan = plan_split("docsplit000000012", [3], cfg)

    original_writer = split_module._write_bytes_exclusive_atomic
    calls = {"count": 0}

    def flaky_writer(path: Path, data: bytes):
        if path.name == "recover-write--part-01.pdf":
            calls["count"] += 1
            if calls["count"] == 1:
                raise SplitError("injected output write failure")
        return original_writer(path, data)

    monkeypatch.setattr(split_module, "_write_bytes_exclusive_atomic", flaky_writer)
    with pytest.raises(SplitError, match="injected output write failure"):
        execute_split(plan, cfg)

    monkeypatch.setattr(split_module, "_write_bytes_exclusive_atomic", original_writer)
    result = execute_split(plan, cfg)
    assert all((cfg.inbox_path / item.filename).exists() for item in result.outputs)


def test_execute_split_recovers_after_final_completion_write_failure(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "recover-complete.pdf", "docsplit000000013", pages=5)
    plan = plan_split("docsplit000000013", [3], cfg)

    original_write_sidecar = split_module._write_sidecar_atomic
    calls = {"count": 0}

    def flaky_write_sidecar(sidecar, sidecar_path):
        calls["count"] += 1
        # Fail completion write after parts are already published.
        if calls["count"] == 4:
            raise SplitError("injected completion write failure")
        return original_write_sidecar(sidecar, sidecar_path)

    monkeypatch.setattr(split_module, "_write_sidecar_atomic", flaky_write_sidecar)
    with pytest.raises(SplitError, match="injected completion write failure"):
        execute_split(plan, cfg)

    assert (cfg.inbox_path / "recover-complete--part-01.pdf").exists()

    monkeypatch.setattr(split_module, "_write_sidecar_atomic", original_write_sidecar)
    result = execute_split(plan, cfg)
    assert len(result.outputs) == 2
    sidecar = read_sidecar(cfg.split_path / "recover-complete.pdf")
    assert sidecar is not None
    assert sidecar.split_operations[0].state == "completed"


def test_exclusive_atomic_write_does_not_overwrite_racing_writer(tmp_path, monkeypatch):
    destination = tmp_path / "part.pdf"
    original_link = os.link

    def racing_link(source, target, *args, **kwargs):
        Path(target).write_bytes(b"concurrent-writer")
        return original_link(source, target, *args, **kwargs)

    monkeypatch.setattr(split_module.os, "link", racing_link)

    with pytest.raises(SplitError, match="Output collision"):
        split_module._write_bytes_exclusive_atomic(destination, b"split-output")

    assert destination.read_bytes() == b"concurrent-writer"


def test_execute_split_marks_batch_pending_before_first_publish_and_cleans_staging(
    tmp_path, monkeypatch
):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "batch.pdf", "docsplit000000014", pages=5)
    plan = plan_split("docsplit000000014", [3], cfg)
    original_publish = split_module._publish_output_from_stage
    observed = {"publish_calls": 0}

    def inspect_publish(staged_path, final_path, *, expected_hash, inbox_root):
        staging_root, journal_path = split_module._staging_paths(cfg, plan.operation_id)
        journal = split_module._load_publish_journal(
            journal_path,
            expected_outputs=tuple(part.destination_filename for part in plan.parts),
        )
        assert journal is not None
        assert journal.state == "publishing"
        if observed["publish_calls"] == 0:
            assert not any(
                (cfg.inbox_path / part.destination_filename).exists() for part in plan.parts
            )
        assert split_module.is_split_output_pending(final_path, cfg.inbox_path)
        assert staging_root.exists()
        observed["publish_calls"] += 1
        return original_publish(
            staged_path,
            final_path,
            expected_hash=expected_hash,
            inbox_root=inbox_root,
        )

    monkeypatch.setattr(split_module, "_publish_output_from_stage", inspect_publish)

    result = execute_split(plan, cfg)
    staging_root, _ = split_module._staging_paths(cfg, plan.operation_id)

    assert observed["publish_calls"] == 2
    assert not staging_root.exists()
    assert not split_module.is_split_output_pending(
        cfg.inbox_path / result.outputs[0].filename, cfg.inbox_path
    )


def test_inbox_scan_skips_output_while_publish_journal_is_active(tmp_path):
    cfg = make_config(tmp_path)
    cfg.inbox_path.mkdir(parents=True, exist_ok=True)
    output_path = cfg.inbox_path / "batch--part-01.pdf"
    output_path.write_bytes(pdf_bytes(1))
    operation_id = "a" * 32
    _, journal_path = split_module._staging_paths(cfg, operation_id)
    split_module._write_publish_journal(
        journal_path,
        state="publishing",
        outputs=(output_path.name,),
        published={output_path.name},
    )

    with patch("aktenfux.main._process_single") as process_single:
        from aktenfux.main import process_inbox

        process_inbox(cfg)
        process_single.assert_not_called()

    split_module._write_publish_journal(
        journal_path,
        state="completed",
        outputs=(output_path.name,),
        published={output_path.name},
    )
    with patch("aktenfux.main._process_single") as process_single:
        process_inbox(cfg)
        process_single.assert_called_once_with(output_path, cfg)


def test_interrupted_publish_is_hidden_from_scanner_and_recovers(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "interrupted.pdf", "docsplit000000016", pages=5)
    plan = plan_split("docsplit000000016", [3], cfg)
    original_publish = split_module._publish_output_from_stage
    publish_calls = {"count": 0}

    def fail_second_publish(staged_path, final_path, *, expected_hash, inbox_root):
        publish_calls["count"] += 1
        if publish_calls["count"] == 2:
            raise SplitError("injected second publish failure")
        return original_publish(
            staged_path,
            final_path,
            expected_hash=expected_hash,
            inbox_root=inbox_root,
        )

    monkeypatch.setattr(split_module, "_publish_output_from_stage", fail_second_publish)
    with pytest.raises(SplitError, match="injected second publish failure"):
        execute_split(plan, cfg)

    first_output = cfg.inbox_path / "interrupted--part-01.pdf"
    second_output = cfg.inbox_path / "interrupted--part-02.pdf"
    assert first_output.exists()
    assert not second_output.exists()
    assert split_module.is_split_output_pending(first_output, cfg.inbox_path)

    with patch("aktenfux.main._process_single") as process_single:
        from aktenfux.main import process_inbox

        process_inbox(cfg)
        process_single.assert_not_called()

    monkeypatch.setattr(split_module, "_publish_output_from_stage", original_publish)
    result = execute_split(plan, cfg)
    staging_root, _ = split_module._staging_paths(cfg, plan.operation_id)

    assert len(result.outputs) == 2
    assert first_output.exists()
    assert second_output.exists()
    assert not staging_root.exists()


def test_parse_utc_rejects_naive_timestamp():
    with pytest.raises(SplitError, match="must be UTC"):
        split_module._parse_utc("2026-10-09T00:00:00", field_name="created_at_utc")


def test_load_publish_journal_rejects_non_object_json(tmp_path):
    journal_path = tmp_path / "publish-state.json"
    journal_path.write_text("[]", encoding="utf-8")

    with pytest.raises(SplitError, match="journal is invalid"):
        split_module._load_publish_journal(journal_path)


def test_execute_split_bounds_directory_creation_errors(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "mkdir.pdf", "docsplit000000015", pages=4)
    plan = plan_split("docsplit000000015", [3], cfg)
    original_mkdir = Path.mkdir

    def failing_mkdir(path, *args, **kwargs):
        if path == cfg.inbox_path:
            raise OSError("failure at /sensitive/full/path")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", failing_mkdir)

    with pytest.raises(SplitError) as exc_info:
        execute_split(plan, cfg)

    assert "inbox" in str(exc_info.value)
    assert "/sensitive/full/path" not in str(exc_info.value)
