"""Tests for storage path safety and file operations."""
from __future__ import annotations

from pathlib import Path

import pytest

from aktenfux.storage import (
    assert_within_base,
    markdown_path_for,
    move_file_with_sidecar,
    sha256_file,
    sidecar_path_for,
)


class TestAssertWithinBase:
    def test_valid_path(self, tmp_path):
        sub = tmp_path / "sub" / "file.pdf"
        assert_within_base(sub, tmp_path)  # Should not raise

    def test_path_traversal_rejected(self, tmp_path):
        outside = tmp_path.parent / "outside" / "file.pdf"
        with pytest.raises(ValueError, match="Path traversal"):
            assert_within_base(outside, tmp_path)

    def test_base_itself_is_valid(self, tmp_path):
        assert_within_base(tmp_path, tmp_path)  # Should not raise

    def test_symlink_traversal(self, tmp_path):
        """A resolved symlink outside base_dir must be rejected."""
        outside = tmp_path.parent / "outside"
        outside.mkdir(exist_ok=True)
        link = tmp_path / "link"
        link.symlink_to(outside)
        with pytest.raises(ValueError, match="Path traversal"):
            assert_within_base(link / "file.pdf", tmp_path)


class TestSidecarPathFor:
    def test_replaces_suffix(self):
        path = Path("/some/dir/document.pdf")
        assert sidecar_path_for(path) == Path("/some/dir/document.json")

    def test_preserves_directory(self):
        path = Path("/a/b/c/file.pdf")
        assert sidecar_path_for(path).parent == Path("/a/b/c")


class TestMarkdownPathFor:
    def test_replaces_suffix(self):
        path = Path("/some/dir/document.pdf")
        assert markdown_path_for(path) == Path("/some/dir/document.md")


class TestSha256File:
    def test_known_hash(self, tmp_path):
        import hashlib

        content = b"Hello, Aktenfux!"
        f = tmp_path / "test.txt"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert sha256_file(f) == expected

    def test_different_files_differ(self, tmp_path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_bytes(b"file A")
        b.write_bytes(b"file B")
        assert sha256_file(a) != sha256_file(b)

    def test_identical_content_same_hash(self, tmp_path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        data = b"same content"
        a.write_bytes(data)
        b.write_bytes(data)
        assert sha256_file(a) == sha256_file(b)


class TestMoveFileWithSidecar:
    def _make_pdf(self, directory: Path, name: str = "doc.pdf") -> Path:
        path = directory / name
        path.write_bytes(b"%PDF-1.4 fake pdf content")
        return path

    def test_dry_run_does_not_move(self, tmp_path):
        src = self._make_pdf(tmp_path, "source.pdf")
        dest = tmp_path / "dest" / "moved.pdf"
        move_file_with_sidecar(src, dest, base_dir=tmp_path, dry_run=True)
        assert src.exists()
        assert not dest.exists()

    def test_actual_move(self, tmp_path):
        src = self._make_pdf(tmp_path, "source.pdf")
        dest = tmp_path / "dest" / "moved.pdf"
        move_file_with_sidecar(src, dest, base_dir=tmp_path, dry_run=False)
        assert not src.exists()
        assert dest.exists()

    def test_sidecar_moved_together(self, tmp_path):
        src = self._make_pdf(tmp_path, "source.pdf")
        sidecar = sidecar_path_for(src)
        sidecar.write_text('{"id": "test"}', encoding="utf-8")
        dest = tmp_path / "dest" / "moved.pdf"
        move_file_with_sidecar(src, dest, base_dir=tmp_path, dry_run=False)
        assert not sidecar.exists()
        assert sidecar_path_for(dest).exists()

    def test_no_overwrite_raises(self, tmp_path):
        src = self._make_pdf(tmp_path, "source.pdf")
        dest = tmp_path / "existing.pdf"
        dest.write_bytes(b"existing content")
        with pytest.raises(FileExistsError):
            move_file_with_sidecar(src, dest, base_dir=tmp_path, dry_run=False)

    def test_creates_parent_directories(self, tmp_path):
        src = self._make_pdf(tmp_path, "source.pdf")
        dest = tmp_path / "deep" / "nested" / "dir" / "moved.pdf"
        move_file_with_sidecar(src, dest, base_dir=tmp_path, dry_run=False)
        assert dest.exists()

    def test_outside_base_rejected(self, tmp_path):
        src = self._make_pdf(tmp_path, "source.pdf")
        outside = tmp_path.parent / "other" / "moved.pdf"
        with pytest.raises(ValueError, match="Path traversal"):
            move_file_with_sidecar(src, outside, base_dir=tmp_path, dry_run=False)
