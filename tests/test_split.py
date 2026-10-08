"""Tests for recoverable split planning/execution services."""
from __future__ import annotations

import pytest
from pypdf import PdfReader

from aktenfux.schema import SidecarDocument
from aktenfux.split import execute_split, plan_split
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

    with pytest.raises(ValueError, match="Duplicate boundaries"):
        plan_split("docsplit000000002", [3, 3], cfg)


def test_plan_split_rejects_single_page_pdf(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "single.pdf", "docsplit000000003", pages=1)

    with pytest.raises(ValueError, match="Single-page PDFs"):
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


def test_execute_split_is_idempotent_retry(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "retry.pdf", "docsplit000000005", pages=4)
    plan = plan_split("docsplit000000005", [3], cfg)

    first = execute_split(plan, cfg)
    second = execute_split(plan, cfg)

    assert [item.sha256 for item in first.outputs] == [item.sha256 for item in second.outputs]


def test_execute_split_fails_if_source_changes_after_plan(tmp_path):
    cfg = make_config(tmp_path)
    source = write_split_doc(cfg.split_path, "mutated.pdf", "docsplit000000006", pages=6)
    plan = plan_split("docsplit000000006", [3], cfg)
    source.write_bytes(pdf_bytes(7))

    with pytest.raises(RuntimeError, match="Source document changed"):
        execute_split(plan, cfg)


def test_execute_split_rejects_untracked_destination_collision(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "collision.pdf", "docsplit000000007", pages=6)
    plan = plan_split("docsplit000000007", [4], cfg)
    cfg.inbox_path.mkdir(parents=True, exist_ok=True)
    (cfg.inbox_path / "collision--part-01.pdf").write_bytes(pdf_bytes(1))

    with pytest.raises(FileExistsError, match="Destination collision"):
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

    sidecar = SidecarDocument(
        id="docsplit000000008",
        original_path=str(symlink_pdf),
        current_path=str(symlink_pdf),
        sha256="a" * 64,
        suggested_filename=symlink_pdf.name,
        status="approved",
    )
    symlink_pdf.with_suffix(".json").write_text(sidecar.model_dump_json(indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match="outside the expected lifecycle root|symbolic link"):
        plan_split("docsplit000000008", [2], cfg)
