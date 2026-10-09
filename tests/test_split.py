"""Tests for recoverable split planning/execution services."""
from __future__ import annotations

import json
from pathlib import Path

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

    with pytest.raises(SplitError, match="outside the expected lifecycle root|symbolic link"):
        plan_split("docsplit000000010", [2], cfg)


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
