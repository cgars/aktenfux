"""CLI tests for safe, predictable workspace initialisation."""
from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from aktenfux.cli import app


runner = CliRunner()


def test_init_target_becomes_new_config_base_dir(tmp_path):
    target = tmp_path / "new workspace #1"
    target.mkdir()

    result = runner.invoke(app, ["init", "--target", str(target)])

    assert result.exit_code == 0, result.output
    assert "Config created" in result.output
    config_path = target / "config.yaml"
    config_text = config_path.read_text(encoding="utf-8")
    assert "# Base directory" in config_text
    payload = yaml.safe_load(config_text)
    assert Path(payload["base_dir"]) == target.resolve()
    for name in (
        "_Inbox",
        "_Review",
        "_Imported",
        "_Error",
        "Archive",
        "_Split",
        "_DryRun",
    ):
        assert (target / name).is_dir()


def test_init_creates_missing_target_directory(tmp_path):
    target = tmp_path / "missing" / "workspace"

    result = runner.invoke(app, ["init", "--target", str(target)])

    assert result.exit_code == 0, result.output
    assert "Config created" in result.output
    assert (target / "config.yaml").is_file()
    assert (target / "_Inbox").is_dir()


def test_init_does_not_overwrite_existing_config_base_dir(tmp_path):
    target = tmp_path / "config-location"
    configured_base = tmp_path / "existing-data"
    target.mkdir()
    config_path = target / "config.yaml"
    original = (
        f'base_dir: "{configured_base}"\n'
        "dry_run: false\n"
        "use_sqlite_index: false\n"
    )
    config_path.write_text(original, encoding="utf-8")

    result = runner.invoke(app, ["init", "--target", str(target)])

    assert result.exit_code == 0, result.output
    assert "Config file ready" in result.output
    assert config_path.read_text(encoding="utf-8") == original
    assert (configured_base / "_Inbox").is_dir()
    assert not (target / "_Inbox").exists()
