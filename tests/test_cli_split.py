"""CLI tests for `afu split`."""
from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from aktenfux.cli import app
from aktenfux.schema import (
    SplitBoundaryCandidate,
    SplitBoundaryEvidence,
    SplitBoundarySuggestion,
    SplitOutputRecord,
    SplitProvenanceRecord,
)
from aktenfux.split import SplitError
from aktenfux.storage import read_sidecar, sha256_file
from tests.split_helpers import make_config, write_split_doc

runner = CliRunner()


def test_split_dry_run_previews_only(tmp_path):
    cfg = make_config(tmp_path)
    cfg.dry_run = True
    write_split_doc(cfg.split_path, "dry.pdf", "docsplitcli000001", pages=5)

    with patch("aktenfux.cli._load_config", return_value=cfg):
        result = runner.invoke(
            app,
            ["split", "docsplitcli000001", "--before", "3"],
        )

    assert result.exit_code == 0, result.output
    assert "pages 1-2 -> _Inbox/dry--part-01.pdf" in result.output
    assert "Dry-run" in result.output
    assert not (cfg.inbox_path / "dry--part-01.pdf").exists()


def test_split_cancel_keeps_state_unchanged(tmp_path):
    cfg = make_config(tmp_path)
    source = write_split_doc(cfg.split_path, "cancel.pdf", "docsplitcli000002", pages=5)

    with patch("aktenfux.cli._load_config", return_value=cfg):
        result = runner.invoke(
            app,
            ["split", "docsplitcli000002", "--before", "3"],
            input="n\n",
        )

    assert result.exit_code == 0, result.output
    assert "Cancelled." in result.output
    assert not (cfg.inbox_path / "cancel--part-01.pdf").exists()
    sidecar = read_sidecar(source)
    assert sidecar is not None
    assert sidecar.split_operations == []


def test_split_execute_after_confirmation(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "run.pdf", "docsplitcli000003", pages=6)

    with patch("aktenfux.cli._load_config", return_value=cfg):
        result = runner.invoke(
            app,
            ["split", "docsplitcli000003", "--before", "4"],
            input="y\n",
        )

    assert result.exit_code == 0, result.output
    assert "Split complete" in result.output
    assert (cfg.inbox_path / "run--part-01.pdf").exists()
    assert (cfg.inbox_path / "run--part-02.pdf").exists()


def test_split_completed_lists_retained_sources(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "retained.pdf", "docsplitcli000003b", pages=6)

    with patch("aktenfux.cli._load_config", return_value=cfg):
        split_result = runner.invoke(
            app,
            ["split", "docsplitcli000003b", "--before", "4", "--yes"],
        )
        assert split_result.exit_code == 0, split_result.output
        result = runner.invoke(app, ["split-completed"])

    assert result.exit_code == 0, result.output
    assert "Retained completed split sources" in result.output
    assert "Operation" in result.output


def test_split_completed_fails_when_retained_entries_are_invalid(tmp_path):
    cfg = make_config(tmp_path)
    cfg.split_path.mkdir(parents=True, exist_ok=True)
    cfg.imported_path.mkdir(parents=True, exist_ok=True)
    (cfg.imported_path / "broken--completed-aaaaaaaaaaaa.pdf").write_bytes(b"%PDF-1.7")

    with patch("aktenfux.cli._load_config", return_value=cfg):
        result = runner.invoke(app, ["split-completed"])

    assert result.exit_code == 1
    assert "inspection found issues" in result.output.lower()


def test_split_completed_fails_when_retained_sidecar_is_orphaned(tmp_path):
    cfg = make_config(tmp_path)
    cfg.split_path.mkdir(parents=True, exist_ok=True)
    cfg.imported_path.mkdir(parents=True, exist_ok=True)
    (cfg.imported_path / "orphan--completed-aaaaaaaaaaaa.json").write_text("{}", encoding="utf-8")

    with patch("aktenfux.cli._load_config", return_value=cfg):
        result = runner.invoke(app, ["split-completed"])

    assert result.exit_code == 1
    assert "no matching pdf" in result.output.lower()


def test_split_reconcile_completed_dry_run_reports_eligible(tmp_path):
    cfg = make_config(tmp_path)
    source = write_split_doc(cfg.split_path, "legacy-cli.pdf", "docsplitcli000003c", pages=5)
    # Build a legacy completed state directly in _Split.
    from aktenfux.main import plan_split  # noqa: PLC0415
    import aktenfux.split as split_module  # noqa: PLC0415

    plan = plan_split("docsplitcli000003c", [3], cfg)
    source_bytes = source.read_bytes()
    outputs = []
    cfg.inbox_path.mkdir(parents=True, exist_ok=True)
    for part in plan.parts:
        payload = split_module._part_pdf_bytes(  # noqa: SLF001
            source_bytes,
            start_page=part.start_page,
            end_page=part.end_page,
        )
        output_path = cfg.inbox_path / part.destination_filename
        output_path.write_bytes(payload)
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

    with patch("aktenfux.cli._load_config", return_value=cfg):
        result = runner.invoke(app, ["split-reconcile-completed"])

    assert result.exit_code == 0, result.output
    assert "Eligible completed sources: 1" in result.output


def test_split_suggest_prints_candidates_without_mutation(tmp_path):
    cfg = make_config(tmp_path)
    source = write_split_doc(cfg.split_path, "suggest.pdf", "docsplitcli000004", pages=6)
    sidecar_before = source.with_suffix(".json").read_text(encoding="utf-8")
    suggestion = SplitBoundarySuggestion(
        document_id="docsplitcli000004",
        source_sha256="a" * 64,
        source_page_count=6,
        suggestion_provider="ollama:qwen3:8b",
        candidates=[
            SplitBoundaryCandidate(
                before_page=4,
                confidence=0.84,
                rationale="A second document starts with a new sender block.",
                evidence=[SplitBoundaryEvidence(page=4, cue="Sender: Contoso GmbH")],
            )
        ],
        warnings=["Page 6 is mostly blank."],
    )

    with (
        patch("aktenfux.cli._load_config", return_value=cfg),
        patch("aktenfux.main.suggest_split_boundaries", return_value=suggestion),
    ):
        result = runner.invoke(app, ["split-suggest", "docsplitcli000004"])

    assert result.exit_code == 0, result.output
    assert "Split before page 4" in result.output
    assert "No files were changed." in result.output
    assert source.with_suffix(".json").read_text(encoding="utf-8") == sidecar_before


def test_split_suggest_reports_no_candidates(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "suggest.pdf", "docsplitcli000005", pages=6)
    suggestion = SplitBoundarySuggestion(
        document_id="docsplitcli000005",
        source_sha256="b" * 64,
        source_page_count=6,
        suggestion_provider="ollama:qwen3:8b",
        candidates=[],
        warnings=["Insufficient page text for reliable boundaries."],
    )

    with (
        patch("aktenfux.cli._load_config", return_value=cfg),
        patch("aktenfux.main.suggest_split_boundaries", return_value=suggestion),
    ):
        result = runner.invoke(app, ["split-suggest", "docsplitcli000005"])

    assert result.exit_code == 0, result.output
    assert "No reliable split boundaries were suggested." in result.output


def test_split_suggest_sanitizes_untrusted_terminal_text(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "suggest.pdf", "docsplitcli000006", pages=6)
    suggestion = SplitBoundarySuggestion(
        document_id="docsplitcli000006",
        source_sha256="c" * 64,
        source_page_count=6,
        suggestion_provider="ollama:[danger]\x1b[31mprovider\x1b[0m",
        candidates=[
            SplitBoundaryCandidate(
                before_page=4,
                confidence=0.84,
                rationale="[bold]line1[/bold]\nline2\twith-tab\x07",
                evidence=[SplitBoundaryEvidence(page=4, cue="cue\x1b]8;;http://evil\x07link\x1b]8;;\x07")],
            )
        ],
        warnings=["[yellow]warn[/yellow]\nsecond-line"],
    )

    with (
        patch("aktenfux.cli._load_config", return_value=cfg),
        patch("aktenfux.main.suggest_split_boundaries", return_value=suggestion),
    ):
        result = runner.invoke(app, ["split-suggest", "docsplitcli000006"])

    assert result.exit_code == 0, result.output
    assert "\x1b[" not in result.output
    assert "[bold]line1[/bold]" in result.output
    assert "line2 with-tab�" in result.output
    assert "warn[/yellow] second-line" in result.output


def test_split_suggest_sanitizes_untrusted_exception_text(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "suggest.pdf", "docsplitcli000007", pages=6)
    hostile = "[bold]spoofed[/bold]\x1b[31mred\x1b[0m\tline\nnext\rreturn\x1b]8;;http://evil\x07link\x1b]8;;\x07\x00"

    with (
        patch("aktenfux.cli._load_config", return_value=cfg),
        patch("aktenfux.main.suggest_split_boundaries", side_effect=SplitError(hostile)),
    ):
        result = runner.invoke(app, ["split-suggest", "docsplitcli000007"])

    assert result.exit_code == 1
    assert "Error: [bold]spoofed[/bold]red line next return" in result.output
    assert "\x1b" not in result.output
    assert "\x07" not in result.output
    assert "\t" not in result.output
    assert "\r" not in result.output
