"""Tests that CLI effect reporting does not claim unobserved mutations."""
from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from aktenfux.cli import app
from aktenfux.config import AktenfuxConfig


runner = CliRunner()


def test_reprocess_dry_run_reports_conditional_effects_as_possible(tmp_path):
    cfg = AktenfuxConfig(
        {
            "base_dir": str(tmp_path),
            "dry_run": True,
            "use_sqlite_index": False,
        }
    )

    with (
        patch("aktenfux.cli._load_config", return_value=cfg),
        patch("aktenfux.ollama_manager.is_ollama_running", return_value=True),
        patch("aktenfux.main.reprocess_document"),
    ):
        result = runner.invoke(app, ["reprocess", "example-id"])

    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert "may have written JSON" in output
    assert "accessed or created SQLite state" in output
    assert "completed with the documented" not in output


def test_reprocess_normal_does_not_claim_an_unobserved_success(tmp_path):
    cfg = AktenfuxConfig(
        {
            "base_dir": str(tmp_path),
            "dry_run": False,
            "use_sqlite_index": False,
        }
    )

    with (
        patch("aktenfux.cli._load_config", return_value=cfg),
        patch("aktenfux.ollama_manager.is_ollama_running", return_value=True),
        patch("aktenfux.main.reprocess_document"),
    ):
        result = runner.invoke(app, ["reprocess", "example-id"])

    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert "does not return an outcome receipt" in output
    assert "before treating it as successful" in output
    assert "✓ Reprocessed" not in output
