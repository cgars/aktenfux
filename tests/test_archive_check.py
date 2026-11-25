from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from pypdf import PdfWriter
from typer.testing import CliRunner

from aktenfux.archive_check import FindingKind, check_archive
from aktenfux.cli import app
from aktenfux.config import AktenfuxConfig
from aktenfux.schema import SidecarDocument
from aktenfux.storage import sha256_file, write_sidecar


@pytest.fixture
def workspace(tmp_path):
    settings = {"base_dir": str(tmp_path), "use_sqlite_index": True, "dry_run": False}
    config = AktenfuxConfig(settings)
    config.archive_path.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    return config, config_path


def _pdf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(path)
    return path


def _pair(path: Path, **updates) -> Path:
    pdf = _pdf(path)
    fields = {
        "id": "synthetic-document",
        "current_path": str(pdf),
        "original_path": str(pdf),
        "sha256": sha256_file(pdf),
        "status": "approved",
    }
    fields.update(updates)
    write_sidecar(SidecarDocument(**fields), pdf)
    return pdf


def _snapshot(workspace: Path):
    return {
        path.relative_to(workspace): path.read_bytes() if path.is_file() else None
        for path in workspace.rglob("*")
    }


def test_cli_checks_nested_archive_without_writing_or_calling_services(workspace):
    config, config_path = workspace
    good = _pair(config.archive_path / "Invoices" / "2026" / "sample.pdf")
    changed = _pair(config.archive_path / "broken [red]  sample.pdf")
    changed.write_bytes(changed.read_bytes() + b"\n% changed")
    before = _snapshot(config.base_dir)

    with (
        patch("aktenfux.db._connect") as connect,
        patch("httpx.Client") as http_client,
        patch("aktenfux.pdf_text.extract_text") as extract_text,
    ):
        args = ["archive-check", "--config", str(config_path)]
        runner = CliRunner()
        result = runner.invoke(app, args)
        details = runner.invoke(app, [*args, "--show-paths"])

    for run in (result, details):
        assert run.exit_code == 1, run.output
        assert "PDF candidates: 2" in run.output
        assert "Verified pairs: 1" in run.output
        assert "hash_mismatch: 1" in run.output
        assert str(config.base_dir) not in run.output
    assert good.name not in result.output
    assert changed.name not in result.output
    assert changed.name in details.output
    assert _snapshot(config.base_dir) == before
    connect.assert_not_called()
    http_client.assert_not_called()
    extract_text.assert_not_called()


def test_cli_distinguishes_empty_from_missing_archive(workspace):
    config, config_path = workspace
    args = ["archive-check", "--config", str(config_path)]
    runner = CliRunner()

    empty = runner.invoke(app, args)
    assert empty.exit_code == 0, empty.output
    assert "PDF candidates: 0" in empty.output

    config.archive_path.rmdir()
    missing = runner.invoke(app, args)
    assert missing.exit_code == 2, missing.output
    assert "could not start" in missing.output
    assert "Verified pairs" not in missing.output


def test_cli_verifies_current_sidecar_without_schema_version(workspace):
    config, config_path = workspace
    _pair(config.archive_path / "sample.pdf")

    result = CliRunner().invoke(app, ["archive-check", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "PDF candidates: 1" in result.output
    assert "Verified pairs: 1" in result.output
    assert "Findings: 0" in result.output


def test_reports_incomplete_malformed_and_stale_pairs(workspace):
    config, _ = workspace
    archive = config.archive_path
    _pdf(archive / "pdf-only.pdf")
    (archive / "json-only.json").write_text("{}", encoding="utf-8")
    invalid = _pdf(archive / "invalid.pdf")
    invalid.with_suffix(".json").write_text("{ invalid", encoding="utf-8")
    oversized = _pdf(archive / "oversized.pdf")
    oversized.with_suffix(".json").write_bytes(b" " * (1024 * 1024 + 1))
    _pair(archive / "old-path.pdf", current_path="old-workspace/old-path.pdf", status="review")
    _pair(archive / "bad-hash.pdf", sha256="unknown")

    result = check_archive(config)

    assert result.pdf_candidates == 5
    assert result.verified_pairs == 0
    assert Counter(f.kind for f in result.findings) == {
        FindingKind.MISSING_SIDECAR: 1,
        FindingKind.MISSING_PDF: 1,
        FindingKind.INVALID_SIDECAR: 2,
        FindingKind.SIDECAR_TOO_LARGE: 1,
        FindingKind.PATH_MISMATCH: 1,
        FindingKind.STATUS_MISMATCH: 1,
    }


@pytest.mark.parametrize("archive_dir", ["../Archive", "nested/Archive", "nested\\Archive"])
def test_cli_rejects_archive_outside_direct_workspace_child(workspace, archive_dir):
    config, config_path = workspace
    config_path.write_text(
        yaml.safe_dump({"base_dir": str(config.base_dir), "archive_dir": archive_dir}),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["archive-check", "--config", str(config_path)])

    assert result.exit_code == 2, result.output
    assert "direct child" in result.output
    assert archive_dir not in result.output


def test_cli_rejects_archive_that_overlaps_review(workspace):
    config, config_path = workspace
    config_path.write_text(
        yaml.safe_dump({"base_dir": str(config.base_dir), "review_dir": "Archive/Review"}),
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["archive-check", "--config", str(config_path)])
    assert result.exit_code == 2, result.output
    assert "overlaps another lifecycle directory" in result.output


def test_cli_rejects_linked_archive_root(workspace):
    config, config_path = workspace
    archive = config.archive_path
    outside = config.base_dir / "SyntheticOtherArchive"
    archive.rename(outside)
    try:
        archive.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory links are unavailable on this filesystem")

    with patch("aktenfux.archive_check.os.open") as open_file:
        result = CliRunner().invoke(app, ["archive-check", "--config", str(config_path)])

    assert result.exit_code == 2, result.output
    assert "directory without links" in " ".join(result.output.split())
    open_file.assert_not_called()


def test_reports_linked_entries_without_reading_them(workspace):
    config, _ = workspace
    archive = config.archive_path
    outside = _pair(config.base_dir / "outside" / "outside.pdf")
    _pdf(archive / "linked.pdf")
    try:
        (archive / "outside-folder").symlink_to(outside.parent, target_is_directory=True)
        (archive / "linked.json").symlink_to(outside.with_suffix(".json"))
        os.link(outside, archive / "hard-link.pdf")
    except OSError:
        pytest.skip("Links are unavailable on this filesystem")

    with patch("aktenfux.archive_check.os.open") as open_file:
        result = check_archive(config)

    assert result.verified_pairs == 0
    assert {f.relative_path.name for f in result.findings if f.kind == FindingKind.UNSAFE_ENTRY} == {
        "linked.json", "outside-folder", "hard-link.pdf",
    }
    open_file.assert_not_called()


def test_reports_case_ambiguous_companions(workspace):
    config, _ = workspace
    original = _pair(config.archive_path / "sample.pdf")
    duplicate = config.archive_path / "Sample.json"
    if duplicate.exists():
        pytest.skip("Filesystem does not distinguish case variants")
    duplicate.write_bytes(original.with_suffix(".json").read_bytes())

    result = check_archive(config)
    assert result.verified_pairs == 0
    assert {finding.kind for finding in result.findings} == {FindingKind.AMBIGUOUS_PAIR}


def test_directory_read_failure_is_a_finding_and_other_pairs_continue(workspace):
    config, _ = workspace
    blocked = config.archive_path / "Unavailable"
    blocked.mkdir()
    _pair(config.archive_path / "sample.pdf")
    original_iterdir = Path.iterdir

    def iterdir(path):
        if path == blocked:
            raise PermissionError("synthetic test failure")
        return original_iterdir(path)

    with patch.object(Path, "iterdir", iterdir):
        result = check_archive(config)

    assert result.verified_pairs == 1
    assert [(f.kind, f.relative_path) for f in result.findings] == [
        (FindingKind.DIRECTORY_UNAVAILABLE, Path("Unavailable"))
    ]


def test_changing_pdf_is_a_finding(workspace):
    config, _ = workspace
    pdf = _pair(config.archive_path / "sample.pdf")
    from aktenfux.storage import sha256_stream

    def change_after_hash(stream):
        digest = sha256_stream(stream)
        with pdf.open("ab") as writer:
            writer.write(b"\n% changed during check")
        return digest

    with patch("aktenfux.archive_check.sha256_stream", side_effect=change_after_hash):
        result = check_archive(config)

    assert result.verified_pairs == 0
    assert [(f.kind, f.relative_path) for f in result.findings] == [
        (FindingKind.UNREADABLE_OR_CHANGED, Path("sample.pdf"))
    ]
