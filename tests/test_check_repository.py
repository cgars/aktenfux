"""Focused tests for repository verification helpers."""

import subprocess
from pathlib import Path

import pytest

from scripts.check_repository import (
    check_git_artifacts,
    check_git_markdown,
    check_named_diagrams,
    check_markdown_links,
    check_markdown_tables,
    check_runtime_artifacts,
    markdown_files,
)
from scripts.render_architecture import discover_diagrams


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


def test_runtime_artifacts_reject_sensitive_generated_paths(tmp_path, monkeypatch):
    disguised = tmp_path / "private.data"
    disguised.write_bytes(b"SQLite format 3\x00" + b"synthetic")
    probed: list[str] = []

    def record_sqlite_probe(path, root):
        probed.append(str(path))
        return path.name == "private.data"

    monkeypatch.setattr(
        "scripts.check_repository._is_sqlite_file", record_sqlite_probe
    )
    errors = check_runtime_artifacts(
        [
            "config.yaml",
            "_Inbox/private.pdf",
            "workspace/_Review/private.json",
            "archive.db",
            "index.sqlite3",
            "index.db-wal",
            "private.data",
            "src/__pycache__/module.pyc",
        ],
        root=tmp_path,
    )

    assert len(errors) == 8
    assert "workspace/_Review/private.json" not in probed
    assert "private.data" in probed


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
        "~~~mermaid\nflowchart TD\nC --> D\n~~~\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts.check_repository.REPO_ROOT", tmp_path)

    errors = check_named_diagrams([source])

    assert len(errors) == 1
    assert "lacks" in errors[0]


def test_renderer_discovers_named_tilde_fence(tmp_path):
    source = tmp_path / "architecture.md"
    source.write_text(
        "<!-- diagram: tilde -->\n~~~mermaid\nflowchart TD\nA --> B\n~~~~\n",
        encoding="utf-8",
    )

    assert discover_diagrams([source]) == [("tilde", "flowchart TD\nA --> B\n")]


def test_git_markdown_inspects_staged_blobs_not_worktree_replacements(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    readme = tmp_path / "README.md"
    architecture = tmp_path / "docs" / "architecture.md"
    architecture.parent.mkdir()
    readme.write_text(
        "[Missing](missing.md)\n\nFirst | Second\n---|---\nonly one |\n",
        encoding="utf-8",
    )
    architecture.write_text(
        "<!-- diagram: named -->\n```mermaid\nflowchart TD\nA --> B\n```\n\n"
        "~~~mermaid\nflowchart TD\nC --> D\n~~~~\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "README.md", "docs/architecture.md"],
        cwd=tmp_path,
        check=True,
    )
    readme.write_text("# Safe replacement\n", encoding="utf-8")
    architecture.write_text(
        "<!-- diagram: named -->\n```mermaid\nflowchart TD\nA --> B\n```\n",
        encoding="utf-8",
    )

    errors = check_git_markdown(root=tmp_path)

    assert any(
        "index README.md" in error and "missing relative link" in error
        for error in errors
    )
    assert any("index README.md" in error and "table has" in error for error in errors)
    assert any(
        "index docs/architecture.md" in error and "lacks" in error
        for error in errors
    )


def test_git_markdown_inspects_committed_blob_not_staged_repair(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    readme = tmp_path / "README.md"
    readme.write_text("[Missing](missing.md)\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Harness Test",
            "-c",
            "user.email=harness@example.invalid",
            "commit",
            "-qm",
            "broken docs",
        ],
        cwd=tmp_path,
        check=True,
    )
    readme.write_text("# Safe staged replacement\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)

    errors = check_git_markdown(root=tmp_path)

    assert any("HEAD README.md" in error and "missing relative link" in error for error in errors)
    assert not any("index README.md" in error for error in errors)


def test_git_artifacts_reject_nested_lifecycle_path(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    candidate = tmp_path / "workspace" / "_Inbox" / "private.pdf"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("synthetic sensitive content", encoding="utf-8")
    subprocess.run(
        ["git", "add", "workspace/_Inbox/private.pdf"], cwd=tmp_path, check=True
    )
    monkeypatch.setattr(
        "scripts.check_repository._git_blob_header",
        lambda sha, root: pytest.fail("lifecycle Git blob content was probed"),
    )

    errors = check_git_artifacts(root=tmp_path)

    assert len(errors) == 1
    assert "index artifact: workspace/_Inbox/private.pdf" in errors[0]


def test_git_artifacts_inspect_staged_blob_not_replacement(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    candidate = tmp_path / "private.data"
    candidate.write_bytes(b"SQLite format 3\x00" + b"synthetic")
    subprocess.run(["git", "add", "private.data"], cwd=tmp_path, check=True)
    candidate.write_text("safe replacement", encoding="utf-8")

    errors = check_git_artifacts(root=tmp_path)

    assert len(errors) == 1
    assert "index artifact: private.data" in errors[0]


def test_git_artifacts_inspect_staged_symlink_mode_not_replacement(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    candidate = tmp_path / "pointer.md"
    try:
        candidate.symlink_to("outside.md")
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    subprocess.run(["git", "add", "pointer.md"], cwd=tmp_path, check=True)
    candidate.unlink()
    candidate.write_text("safe replacement", encoding="utf-8")

    errors = check_git_artifacts(root=tmp_path)

    assert len(errors) == 1
    assert "index artifact: pointer.md" in errors[0]
