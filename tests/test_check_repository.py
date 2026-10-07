"""Focused tests for repository verification helpers."""
from pathlib import Path

from scripts.check_repository import (
    check_markdown_links,
    check_markdown_tables,
    check_runtime_artifacts,
)


def test_markdown_links_accept_existing_relative_target(tmp_path, monkeypatch):
    source = tmp_path / "README.md"
    target = tmp_path / "docs" / "architecture.md"
    target.parent.mkdir()
    target.write_text("# Architecture\n", encoding="utf-8")
    source.write_text("[Architecture](docs/architecture.md)\n", encoding="utf-8")
    monkeypatch.setattr("scripts.check_repository.REPO_ROOT", tmp_path)

    assert check_markdown_links([source]) == []


def test_markdown_links_reject_missing_relative_target(tmp_path, monkeypatch):
    source = tmp_path / "README.md"
    source.write_text("[Missing](docs/missing.md)\n", encoding="utf-8")
    monkeypatch.setattr("scripts.check_repository.REPO_ROOT", tmp_path)

    errors = check_markdown_links([source])

    assert len(errors) == 1
    assert "docs/missing.md" in errors[0]


def test_markdown_tables_reject_inconsistent_rows(tmp_path, monkeypatch):
    source = tmp_path / "README.md"
    source.write_text(
        "| First | Second |\n|---|---|\n| only one |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts.check_repository.REPO_ROOT", tmp_path)

    errors = check_markdown_tables([source])

    assert len(errors) == 1
    assert "expected 2" in errors[0]


def test_runtime_artifacts_reject_sensitive_generated_paths():
    errors = check_runtime_artifacts(
        [
            "config.yaml",
            "_Inbox/private.pdf",
            "archive.db",
            "src/__pycache__/module.pyc",
        ]
    )

    assert len(errors) == 4


def test_runtime_artifacts_allow_synthetic_test_files():
    assert check_runtime_artifacts(
        ["config.example.yaml", "tests/fixtures/synthetic.pdf"]
    ) == []
