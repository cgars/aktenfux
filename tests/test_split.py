"""Tests for recoverable split planning/execution services."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from pypdf import PdfReader
from pypdf import PdfWriter

import aktenfux.split as split_module
from aktenfux.schema import (
    SplitBoundaryCandidate,
    SplitBoundaryEvidence,
    SplitBoundarySuggestion,
    SplitOutputRecord,
    SplitProvenanceRecord,
)
from aktenfux.split import SplitError, execute_split, plan_split, suggest_split_boundaries
from aktenfux.split import (
    list_completed_split_documents,
    list_split_documents,
    reconcile_completed_split_sources,
)
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
    retained_pdf = cfg.imported_path / f"source--completed-{result.operation_id[:12]}.pdf"
    assert not source.exists()
    assert sha256_file(retained_pdf) == source_hash_before

    sidecar = read_sidecar(retained_pdf)
    assert sidecar is not None
    assert len(sidecar.split_operations) == 1
    operation = sidecar.split_operations[0]
    assert operation.operation_id == plan.operation_id
    assert operation.state == "completed"
    assert operation.completed_at_utc is not None
    assert all(output.sha256 is not None for output in operation.outputs)


def test_execute_split_moves_completed_source_to_imported_and_rejects_new_plan(tmp_path):
    cfg = make_config(tmp_path)
    source = write_split_doc(cfg.split_path, "source.pdf", "docsplit000000004b", pages=5)
    plan = plan_split("docsplit000000004b", [3], cfg)

    result = execute_split(plan, cfg)

    retained_pdf = cfg.imported_path / f"source--completed-{result.operation_id[:12]}.pdf"
    retained_sidecar = retained_pdf.with_suffix(".json")
    assert not source.exists()
    assert not source.with_suffix(".json").exists()
    assert retained_pdf.exists()
    assert retained_sidecar.exists()
    assert list_split_documents(cfg) == []
    completed = list_completed_split_documents(cfg)
    assert [entry.document_id for entry in completed] == ["docsplit000000004b"]
    with pytest.raises(SplitError, match="already completed"):
        plan_split("docsplit000000004b", [4], cfg)
    with pytest.raises(SplitError, match="already completed"):
        suggest_split_boundaries("docsplit000000004b", cfg, provider=lambda *_: {"candidates": []})


def test_execute_split_exact_retry_reads_retained_completed_source(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "retry.pdf", "docsplit000000004c", pages=5)
    plan = plan_split("docsplit000000004c", [3], cfg)

    first = execute_split(plan, cfg)
    second = execute_split(plan, cfg)

    assert first.operation_id == second.operation_id
    assert [entry.filename for entry in second.outputs] == [entry.filename for entry in first.outputs]


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

    with pytest.raises(SplitError, match="authoritative sidecar"):
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


def _write_legacy_completed_split_in_split_root(cfg, *, name: str, doc_id: str):
    source = write_split_doc(cfg.split_path, name, doc_id, pages=5)
    plan = plan_split(doc_id, [3], cfg)
    source_bytes = source.read_bytes()
    outputs = []
    cfg.inbox_path.mkdir(parents=True, exist_ok=True)
    for part in plan.parts:
        part_payload = split_module._part_pdf_bytes(  # noqa: SLF001
            source_bytes,
            start_page=part.start_page,
            end_page=part.end_page,
        )
        output_path = cfg.inbox_path / part.destination_filename
        output_path.write_bytes(part_payload)
        outputs.append(
            SplitOutputRecord(
                part_number=part.part_number,
                start_page=part.start_page,
                end_page=part.end_page,
                filename=part.destination_filename,
                sha256=sha256_file(output_path),
            )
        )
    sidecar = read_sidecar(source)
    assert sidecar is not None
    sidecar.split_operations = [
        SplitProvenanceRecord(
            operation_id=plan.operation_id,
            source_sha256=plan.source_sha256,
            source_page_count=plan.source_page_count,
            boundaries=plan.boundaries,
            outputs=outputs,
            state="completed",
            created_at_utc="2026-10-09T00:00:00Z",
            completed_at_utc="2026-10-09T00:10:00Z",
        )
    ]
    source.with_suffix(".json").write_text(sidecar.model_dump_json(indent=2), encoding="utf-8")
    return source, plan


def test_reconcile_completed_split_sources_supports_dry_run_and_apply(tmp_path):
    cfg = make_config(tmp_path)
    source, plan = _write_legacy_completed_split_in_split_root(
        cfg,
        name="legacy.pdf",
        doc_id="docsplitlegacy00001",
    )

    dry_run = reconcile_completed_split_sources(cfg, apply_changes=False)
    assert len(dry_run.eligible) == 1
    assert dry_run.transitioned == ()
    assert dry_run.issues == ()
    assert source.exists()

    apply = reconcile_completed_split_sources(cfg, apply_changes=True)
    assert len(apply.transitioned) == 1
    retained_pdf = cfg.imported_path / f"legacy--completed-{plan.operation_id[:12]}.pdf"
    assert retained_pdf.exists()
    assert not source.exists()


def test_reconcile_completed_split_sources_reports_missing_outputs(tmp_path):
    cfg = make_config(tmp_path)
    _source, _plan = _write_legacy_completed_split_in_split_root(
        cfg,
        name="legacy-missing.pdf",
        doc_id="docsplitlegacy00002",
    )
    first_output = next(cfg.inbox_path.glob("legacy-missing--part-01.pdf"))
    first_output.unlink()

    result = reconcile_completed_split_sources(cfg, apply_changes=False)
    assert len(result.eligible) == 0
    assert len(result.issues) == 1
    assert "missing output" in result.issues[0].reason


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

    original_writer = split_module._write_bytes_exclusive
    calls = {"count": 0}

    def flaky_writer(path: Path, data: bytes):
        if path.name == "recover-write--part-01.pdf":
            calls["count"] += 1
            if calls["count"] == 1:
                raise SplitError("injected output write failure")
        return original_writer(path, data)

    monkeypatch.setattr(split_module, "_write_bytes_exclusive", flaky_writer)
    with pytest.raises(SplitError, match="injected output write failure"):
        execute_split(plan, cfg)

    monkeypatch.setattr(split_module, "_write_bytes_exclusive", original_writer)
    result = execute_split(plan, cfg)
    assert all((cfg.inbox_path / item.filename).exists() for item in result.outputs)


def test_suggest_split_boundaries_accepts_strict_sorted_candidates(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docsuggest00000001", pages=6)

    def provider(prompt, config):
        return {
            "candidates": [
                {
                    "before_page": 3,
                    "confidence": 0.88,
                    "rationale": "The sender and invoice number reset on page 3.",
                    "evidence": [{"page": 3, "cue": "Invoice no. 2026-33"}],
                },
                {
                    "before_page": 5,
                    "confidence": 0.6,
                    "rationale": "A new salutation begins on page 5.",
                    "evidence": [{"page": 5, "cue": "Dear Ms. Doe"}],
                },
            ],
            "warnings": ["Page 6 has little extractable text."],
        }

    result = suggest_split_boundaries("docsuggest00000001", cfg, provider=provider)

    assert result.document_id == "docsuggest00000001"
    assert result.source_page_count == 6
    assert [item.before_page for item in result.candidates] == [3, 5]
    assert "Page 6 has little extractable text." in result.warnings


def test_suggest_split_boundaries_sends_exact_before_page_semantics(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docsuggestsemantics01", pages=6)
    captured_instructions = {}

    def provider(prompt, config):
        captured_instructions.update(prompt["instructions"])
        return {
            "candidates": [
                {
                    "before_page": 4,
                    "confidence": 0.99,
                    "rationale": "The invoice ends on page 3 and the contract starts on page 4.",
                    "evidence": [
                        {"page": 3, "cue": "ENDE DER RECHNUNG"},
                        {"page": 4, "cue": "MIETVERTRAG LASTENRAD"},
                    ],
                }
            ],
            "warnings": [],
        }

    result = suggest_split_boundaries("docsuggestsemantics01", cfg, provider=provider)

    semantics = captured_instructions["boundary_semantics"]
    assert "first physical PDF page of the new logical document" in semantics
    assert "previous document ends on page 3" in semantics
    assert "new document starts on page 4" in semantics
    assert "return before_page 4, never 3" in semantics
    assert result.candidates[0].before_page == 4


def test_suggest_split_boundaries_rejects_unsorted_provider_candidates(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docsuggest00000001b", pages=6)

    def provider(prompt, config):
        return {
            "candidates": [
                {
                    "before_page": 5,
                    "confidence": 0.6,
                    "rationale": "A new salutation begins on page 5.",
                    "evidence": [{"page": 5, "cue": "Dear Ms. Doe"}],
                },
                {
                    "before_page": 3,
                    "confidence": 0.88,
                    "rationale": "The sender and invoice number reset on page 3.",
                    "evidence": [{"page": 3, "cue": "Invoice no. 2026-33"}],
                },
            ],
            "warnings": [],
        }

    with pytest.raises(SplitError, match="invalid split suggestion payload"):
        suggest_split_boundaries("docsuggest00000001b", cfg, provider=provider)


def test_suggest_split_boundaries_rejects_invalid_candidate_boundaries(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docsuggest00000002", pages=6)

    def provider(prompt, config):
        return {
            "candidates": [
                {
                    "before_page": 1,
                    "confidence": 0.9,
                    "rationale": "invalid boundary",
                    "evidence": [{"page": 1, "cue": "invalid"}],
                }
            ]
        }

    with pytest.raises(SplitError, match="invalid split suggestion payload"):
        suggest_split_boundaries("docsuggest00000002", cfg, provider=provider)


@pytest.mark.parametrize(
    "provider_payload",
    [
        {
            "candidates": [
                {
                    "before_page": "4",
                    "confidence": 0.5,
                    "rationale": "string boundary",
                    "evidence": [{"page": 4, "cue": "cue"}],
                }
            ],
            "warnings": [],
        },
        {
            "candidates": [
                {
                    "before_page": 4,
                    "confidence": True,
                    "rationale": "bool confidence",
                    "evidence": [{"page": 4, "cue": "cue"}],
                }
            ],
            "warnings": [],
        },
        {
            "candidates": [
                {
                    "before_page": 4,
                    "confidence": 85,
                    "rationale": "percentage confidence",
                    "evidence": [{"page": 4, "cue": "cue"}],
                }
            ],
            "warnings": [],
        },
        {
            "candidates": [
                {
                    "before_page": 4,
                    "confidence": 0.5,
                    "rationale": "   ",
                    "evidence": [{"page": 4, "cue": "cue"}],
                }
            ],
            "warnings": [],
        },
        {
            "candidates": [
                {
                    "before_page": 4,
                    "confidence": 0.5,
                    "rationale": "ok",
                    "evidence": [{"page": 4, "cue": "   "}],
                }
            ],
            "warnings": [],
        },
        {
            "candidates": [
                {
                    "before_page": 4,
                    "confidence": 0.5,
                    "rationale": "ok",
                    "evidence": [{"page": 7, "cue": "out of range"}],
                }
            ],
            "warnings": [],
        },
        {
            "candidates": [
                {
                    "before_page": 4,
                    "confidence": float("nan"),
                    "rationale": "nan",
                    "evidence": [{"page": 4, "cue": "cue"}],
                }
            ],
            "warnings": [],
        },
        {
            "candidates": [
                {
                    "before_page": 4,
                    "confidence": 0.5,
                    "rationale": "ok",
                    "evidence": [{"page": 4, "cue": "cue"}],
                    "unexpected": "extra",
                }
            ],
            "warnings": [],
        },
        {
            "candidates": [],
            "warnings": ["   "],
        },
        {
            "candidates": [],
            "warnings": [],
            "unexpected": "top-level",
        },
        {
            "candidates": [
                {
                    "before_page": 4,
                    "confidence": 0.5,
                    "rationale": "ok",
                    "evidence": [{"page": 4, "cue": "cue"}],
                },
                {
                    "before_page": 4,
                    "confidence": 0.6,
                    "rationale": "duplicate",
                    "evidence": [{"page": 4, "cue": "cue"}],
                },
            ],
            "warnings": [],
        },
    ],
)
def test_suggest_split_boundaries_rejects_adversarial_provider_payload(tmp_path, provider_payload):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docsuggest00000002b", pages=6)

    def provider(prompt, config):
        return provider_payload

    with pytest.raises(SplitError, match="invalid split suggestion payload"):
        suggest_split_boundaries("docsuggest00000002b", cfg, provider=provider)


def test_suggest_split_boundaries_never_mutates_sidecar(tmp_path):
    cfg = make_config(tmp_path)
    source = write_split_doc(cfg.split_path, "bundle.pdf", "docsuggest00000003", pages=6)
    sidecar_before = source.with_suffix(".json").read_text(encoding="utf-8")

    def provider(prompt, config):
        return {"candidates": []}

    result = suggest_split_boundaries("docsuggest00000003", cfg, provider=provider)

    assert result.candidates == []
    assert source.with_suffix(".json").read_text(encoding="utf-8") == sidecar_before


def test_suggest_split_boundaries_rejects_stale_source_after_provider_response(tmp_path):
    cfg = make_config(tmp_path)
    source = write_split_doc(cfg.split_path, "bundle.pdf", "docsuggest00000004", pages=6)

    def provider(prompt, config):
        source.write_bytes(source.read_bytes() + b"stale")
        return {"candidates": [], "warnings": []}

    with pytest.raises(SplitError, match="authoritative sidecar|became stale"):
        suggest_split_boundaries("docsuggest00000004", cfg, provider=provider)


def test_suggest_split_boundaries_rejects_provider_limit_violations(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docsuggest00000005", pages=6)

    oversized_rationale = "x" * 241
    oversized_warning = "w" * 241
    too_many_evidence = [{"page": 4, "cue": f"cue-{idx}"} for idx in range(9)]
    too_many_candidates = [
        {
            "before_page": 2 if idx % 2 == 0 else 3,
            "confidence": 0.5,
            "rationale": f"candidate-{idx}",
            "evidence": [],
        }
        for idx in range(65)
    ]
    payloads = [
        {
            "candidates": [
                {
                    "before_page": 4,
                    "confidence": 0.5,
                    "rationale": oversized_rationale,
                    "evidence": [],
                }
            ],
            "warnings": [],
        },
        {
            "candidates": [
                {
                    "before_page": 4,
                    "confidence": 0.5,
                    "rationale": "ok",
                    "evidence": too_many_evidence,
                }
            ],
            "warnings": [],
        },
        {"candidates": [], "warnings": [oversized_warning]},
        {"candidates": too_many_candidates, "warnings": []},
    ]

    for payload in payloads:
        def provider(prompt, config, returned=payload):
            return returned

        with pytest.raises(SplitError, match="invalid split suggestion payload"):
            suggest_split_boundaries("docsuggest00000005", cfg, provider=provider)


def test_suggest_split_boundaries_budgets_extraction_warnings(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "blank.pdf", "docsuggest00000006", pages=33)
    provider_calls = {"count": 0}

    def provider(prompt, config):
        provider_calls["count"] += 1
        return {"candidates": [], "warnings": []}

    result = suggest_split_boundaries("docsuggest00000006", cfg, provider=provider)

    assert provider_calls["count"] == 1
    assert len(result.warnings) == 32
    assert "additional split-suggestion warning(s) were omitted" in result.warnings[-1]


def test_suggest_split_boundaries_budgets_combined_extraction_and_provider_warnings(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "warn.pdf", "docsuggest00000007", pages=4)

    synthetic_evidence = [
        split_module.PageEvidence(page=1, text="one", truncated=False, missing=False),
        split_module.PageEvidence(page=2, text="two", truncated=False, missing=False),
    ]
    extraction_warnings = [f"extract-{idx:02d}" for idx in range(30)]
    monkeypatch.setattr(
        split_module,
        "_bounded_page_evidence",
        lambda snapshot: (synthetic_evidence, extraction_warnings.copy()),
    )

    def provider(prompt, config):
        return {"candidates": [], "warnings": [f"provider-{idx:02d}" for idx in range(5)]}

    result = suggest_split_boundaries("docsuggest00000007", cfg, provider=provider)

    assert len(result.warnings) == 32
    assert result.warnings[:30] == extraction_warnings
    assert result.warnings[30] == "provider-00"
    assert "additional split-suggestion warning(s) were omitted" in result.warnings[-1]


def test_suggest_split_boundaries_ascii_request_budget_stays_within_cap(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "ascii.pdf", "docsuggest00000008", pages=64)

    synthetic_evidence = [
        split_module.PageEvidence(page=idx, text="A" * 1200, truncated=False, missing=False)
        for idx in range(1, 65)
    ]
    monkeypatch.setattr(
        split_module,
        "_bounded_page_evidence",
        lambda snapshot: (synthetic_evidence, []),
    )

    captured_payload: dict[str, object] = {}

    def provider(prompt, config):
        captured_payload.update(prompt)
        return {"candidates": [], "warnings": []}

    suggest_split_boundaries("docsuggest00000008", cfg, provider=provider)

    serialized = json.dumps(
        captured_payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert len(serialized) <= split_module._SUGGEST_MAX_PAYLOAD_BYTES
    assert any(page["truncated"] for page in captured_payload["pages"])


def test_suggest_split_boundaries_unicode_request_budget_stays_within_cap(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "unicode.pdf", "docsuggest00000009", pages=64)

    synthetic_evidence = [
        split_module.PageEvidence(page=idx, text="ä" * 1200, truncated=False, missing=False)
        for idx in range(1, 65)
    ]
    monkeypatch.setattr(
        split_module,
        "_bounded_page_evidence",
        lambda snapshot: (synthetic_evidence, []),
    )

    captured_payload: dict[str, object] = {}

    def provider(prompt, config):
        captured_payload.update(prompt)
        return {"candidates": [], "warnings": []}

    suggest_split_boundaries("docsuggest00000009", cfg, provider=provider)

    serialized = json.dumps(
        captured_payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert len(serialized) <= split_module._SUGGEST_MAX_PAYLOAD_BYTES
    assert any(page["truncated"] for page in captured_payload["pages"])


@pytest.mark.parametrize(
    "provider_payload",
    [
        {
            "candidates": [
                {
                    "before_page": 65,
                    "confidence": 0.7,
                    "rationale": "outside analysed range",
                    "evidence": [{"page": 64, "cue": "cue"}],
                }
            ],
            "warnings": [],
        },
        {
            "candidates": [
                {
                    "before_page": 64,
                    "confidence": 0.7,
                    "rationale": "outside analysed evidence range",
                    "evidence": [{"page": 65, "cue": "cue"}],
                }
            ],
            "warnings": [],
        },
    ],
)
def test_suggest_split_boundaries_rejects_provider_pages_outside_analysed_range(
    tmp_path, provider_payload
):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "long.pdf", "docsuggest00000010", pages=80)

    def provider(prompt, config):
        return provider_payload

    with pytest.raises(SplitError, match="outside analysed page range"):
        suggest_split_boundaries("docsuggest00000010", cfg, provider=provider)


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
    retained_pdf = cfg.imported_path / f"recover-complete--completed-{result.operation_id[:12]}.pdf"
    sidecar = read_sidecar(retained_pdf)
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
        split_module._write_bytes_exclusive(destination, b"split-output")

    assert destination.read_bytes() == b"concurrent-writer"


def test_plan_split_rejects_absolute_split_root_before_pdf_parse(tmp_path):
    base_dir = tmp_path / "workspace"
    outside_split = tmp_path / "outside-split"
    cfg = split_module.AktenfuxConfig(
        {
            "base_dir": str(base_dir),
            "split_dir": str(outside_split),
            "dry_run": False,
            "use_sqlite_index": False,
        }
    )
    write_split_doc(outside_split, "outside.pdf", "docsplit000000017", pages=3)

    with patch("aktenfux.split.PdfReader") as pdf_reader:
        with pytest.raises(SplitError, match="direct child of base_dir"):
            plan_split("docsplit000000017", [2], cfg)
        pdf_reader.assert_not_called()


def test_execute_split_rejects_escaped_inbox_before_mutation(tmp_path):
    base_dir = tmp_path / "workspace"
    cfg = split_module.AktenfuxConfig(
        {
            "base_dir": str(base_dir),
            "inbox_dir": "../outside-inbox",
            "dry_run": False,
            "use_sqlite_index": False,
        }
    )
    source = write_split_doc(cfg.split_path, "escaped.pdf", "docsplit000000018", pages=4)
    plan = plan_split("docsplit000000018", [3], cfg)
    sidecar_before = source.with_suffix(".json").read_bytes()

    with pytest.raises(SplitError, match="inbox root must be a direct child"):
        execute_split(plan, cfg)

    assert source.with_suffix(".json").read_bytes() == sidecar_before
    assert not cfg.inbox_path.exists()


def test_plan_split_rejects_linked_split_root_before_pdf_parse(tmp_path):
    base_dir = tmp_path / "workspace"
    outside_split = tmp_path / "outside-linked-split"
    outside_split.mkdir()
    base_dir.mkdir()
    linked_split = base_dir / "_Split"
    try:
        linked_split.symlink_to(outside_split, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("Directory symlinks are not available on this platform.")
    cfg = make_config(base_dir)
    write_split_doc(outside_split, "linked.pdf", "docsplit000000021", pages=3)

    with patch("aktenfux.split.PdfReader") as pdf_reader:
        with pytest.raises(SplitError, match="split root"):
            plan_split("docsplit000000021", [2], cfg)
        pdf_reader.assert_not_called()


def test_execute_split_rejects_aliased_split_and_inbox_roots(tmp_path):
    cfg = split_module.AktenfuxConfig(
        {
            "base_dir": str(tmp_path),
            "inbox_dir": "_Split",
            "dry_run": False,
            "use_sqlite_index": False,
        }
    )
    source = write_split_doc(cfg.split_path, "alias.pdf", "docsplit000000022", pages=4)
    plan = plan_split("docsplit000000022", [3], cfg)
    sidecar_before = source.with_suffix(".json").read_bytes()

    with pytest.raises(SplitError, match="must be distinct"):
        execute_split(plan, cfg)

    assert source.with_suffix(".json").read_bytes() == sidecar_before


def test_plan_split_rejects_pdf_hash_mismatch_before_parse(tmp_path):
    cfg = make_config(tmp_path)
    source = write_split_doc(cfg.split_path, "replaced.pdf", "docsplit000000019", pages=3)
    source.write_bytes(pdf_bytes(4))

    with patch("aktenfux.split.PdfReader") as pdf_reader:
        with pytest.raises(SplitError, match="authoritative sidecar"):
            plan_split("docsplit000000019", [2], cfg)
        pdf_reader.assert_not_called()


def test_exclusive_write_falls_back_without_hard_link_support(tmp_path, monkeypatch):
    destination = tmp_path / "fallback.pdf"

    def unsupported_link(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

    monkeypatch.setattr(split_module.os, "link", unsupported_link)

    split_module._write_bytes_exclusive(destination, b"split-output")

    assert destination.read_bytes() == b"split-output"


def test_exclusive_fallback_does_not_overwrite_collision(tmp_path, monkeypatch):
    destination = tmp_path / "fallback-collision.pdf"
    destination.write_bytes(b"existing")

    def unsupported_link(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

    monkeypatch.setattr(split_module.os, "link", unsupported_link)

    with pytest.raises(SplitError, match="Output collision"):
        split_module._write_bytes_exclusive(destination, b"split-output")

    assert destination.read_bytes() == b"existing"


def test_exclusive_fallback_removes_owned_file_after_write_failure(tmp_path, monkeypatch):
    destination = tmp_path / "fallback-failure.pdf"

    def failing_fsync(fd):
        raise OSError("injected fallback flush failure")

    monkeypatch.setattr(split_module.os, "fsync", failing_fsync)

    with pytest.raises(SplitError, match="Could not write split output"):
        split_module._write_bytes_exclusive_fallback(destination, b"partial")

    assert not destination.exists()


def test_exclusive_write_falls_back_when_link_api_is_unavailable(tmp_path, monkeypatch):
    destination = tmp_path / "fallback-api.pdf"

    def unavailable_link(*args, **kwargs):
        raise NotImplementedError

    monkeypatch.setattr(split_module.os, "link", unavailable_link)

    split_module._write_bytes_exclusive(destination, b"split-output")

    assert destination.read_bytes() == b"split-output"


def test_execute_split_works_without_hard_link_support(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "portable.pdf", "docsplit000000020", pages=5)
    plan = plan_split("docsplit000000020", [3], cfg)

    def unsupported_link(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

    monkeypatch.setattr(split_module.os, "link", unsupported_link)

    result = execute_split(plan, cfg)

    assert all((cfg.inbox_path / output.filename).is_file() for output in result.outputs)
    staging_root, _ = split_module._staging_paths(cfg, plan.operation_id)
    assert not staging_root.exists()


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
