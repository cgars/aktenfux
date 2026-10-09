"""CLI tests for `afu split`."""
from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from aktenfux.cli import app
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
