"""Focused tests for repository verification helpers."""
from pathlib import Path

from scripts.check_repository import (
    check_named_diagrams,
    check_markdown_links,
    check_markdown_tables,
    check_runtime_artifacts,
    markdown_files,
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


def test_markdown_links_reject_missing_reference_target(tmp_path, monkeypatch):
    source = tmp_path / "README.md"
    source.write_text(
        "[Architecture][arch]\n\n[arch]: docs/missing.md\n", encoding="utf-8"
    )
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


def test_markdown_tables_accept_optional_leading_pipes(tmp_path, monkeypatch):
    source = tmp_path / "README.md"
    source.write_text(
        "First | Second\n---|---\none | two\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts.check_repository.REPO_ROOT", tmp_path)

    assert check_markdown_tables([source]) == []


def test_markdown_tables_reject_bad_row_without_leading_pipe(tmp_path, monkeypatch):
    source = tmp_path / "README.md"
    source.write_text(
        "First | Second\n---|---\nonly one |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts.check_repository.REPO_ROOT", tmp_path)

    errors = check_markdown_tables([source])

    assert len(errors) == 1
    assert "expected 2" in errors[0]


def test_runtime_artifacts_reject_sensitive_generated_paths(tmp_path):
    disguised = tmp_path / "private.data"
    disguised.write_bytes(b"SQLite format 3\x00" + b"synthetic")
    errors = check_runtime_artifacts(
        [
            "config.yaml",
            "_Inbox/private.pdf",
            "archive.db",
            "index.sqlite3",
            "index.db-wal",
            "private.data",
            "src/__pycache__/module.pyc",
        ],
        root=tmp_path,
    )

    assert len(errors) == 7


def test_runtime_artifacts_allow_synthetic_test_files():
    assert check_runtime_artifacts(
        ["config.example.yaml", "tests/fixtures/synthetic.pdf"]
    ) == []


def test_markdown_file_inventory_uses_git_selected_paths(tmp_path):
    tracked = tmp_path / "README.md"
    ignored = tmp_path / "venv" / "PACKAGE.md"
    tracked.write_text("# Tracked\n", encoding="utf-8")
    ignored.parent.mkdir()
    ignored.write_text("# Ignored\n", encoding="utf-8")

    assert markdown_files(tmp_path, ["README.md"]) == [tracked]


def test_named_diagrams_reject_unmarked_mermaid_block(tmp_path, monkeypatch):
    source = tmp_path / "architecture.md"
    source.write_text(
        "<!-- diagram: named -->\n```mermaid\nflowchart TD\nA --> B\n```\n\n"
        "```mermaid\nflowchart TD\nC --> D\n```\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts.check_repository.REPO_ROOT", tmp_path)

    errors = check_named_diagrams([source])

    assert len(errors) == 1
    assert "lacks" in errors[0]
