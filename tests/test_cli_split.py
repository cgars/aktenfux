"""CLI tests for `afu split`."""
from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from aktenfux.cli import app
from aktenfux.schema import (
    SplitBoundaryCandidate,
    SplitBoundaryEvidence,
    SplitBoundarySuggestion,
)
from aktenfux.storage import read_sidecar
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
