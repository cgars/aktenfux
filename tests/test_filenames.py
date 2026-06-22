"""Tests for aktenfuchs/filenames.py."""
from __future__ import annotations

from pathlib import Path

import pytest

from aktenfuchs.filenames import (
    make_suggested_filename,
    make_suggested_folder,
    normalize_unicode,
    resolve_collision,
    sanitize_component,
    sanitize_path_component,
)


class TestSanitizeComponent:
    def test_removes_colon(self):
        assert ":" not in sanitize_component("Hello: World")

    def test_removes_slash(self):
        assert "/" not in sanitize_component("foo/bar")
        assert "\\" not in sanitize_component("foo\\bar")

    def test_preserves_umlauts(self):
        result = sanitize_component("Müller & Söhne GmbH")
        assert "ü" in result
        assert "ö" in result

    def test_collapses_whitespace(self):
        result = sanitize_component("hello   world")
        assert "  " not in result

    def test_strips_leading_trailing(self):
        assert sanitize_component("  hello  ") == "hello"

    def test_empty_string(self):
        assert sanitize_component("") == ""

    def test_special_characters_removed(self):
        result = sanitize_component('Invoice #123 @ "test"')
        assert '"' not in result
        assert "@" not in result


class TestSanitizePathComponent:
    def test_spaces_become_hyphens(self):
        result = sanitize_path_component("Hello World")
        assert " " not in result
        assert "-" in result

    def test_umlauts_preserved(self):
        result = sanitize_path_component("Versicherung Österreich")
        assert "ö" in result or "Ö" in result or "ster" in result


class TestMakeSuggestedFilename:
    def test_full_metadata(self):
        result = make_suggested_filename(
            document_date="2026-06-20",
            correspondent="HUK-COBURG",
            document_type="Invoice",
            topic="Car Insurance",
        )
        assert result.startswith("2026-06-20_")
        assert "HUK-COBURG" in result
        assert "Invoice" in result
        assert result.endswith(".pdf")

    def test_no_date_fallback(self):
        result = make_suggested_filename(
            document_date=None,
            correspondent="Test GmbH",
            document_type="Contract",
            topic="Service Agreement",
        )
        assert result.startswith("no_date_")

    def test_no_correspondent_fallback(self):
        result = make_suggested_filename(
            document_date="2026-01-01",
            correspondent=None,
            document_type="Letter",
            topic="General",
        )
        assert "Unknown" in result

    def test_long_filename_truncated(self):
        long_topic = "A" * 200
        result = make_suggested_filename(
            document_date="2026-01-01",
            correspondent="Sender",
            document_type="Invoice",
            topic=long_topic,
        )
        # stem should be at most _MAX_STEM_LENGTH characters + extension
        assert len(result) <= 125  # 120 + len(".pdf")

    def test_unsafe_characters_removed(self):
        result = make_suggested_filename(
            document_date="2026-01-01",
            correspondent='Invoice: "Test/Corp"',
            document_type="Receipt",
            topic="Payment*Done?",
        )
        for bad in [":", '"', "/", "\\", "*", "?"]:
            assert bad not in result

    def test_custom_extension(self):
        result = make_suggested_filename(
            document_date="2026-01-01",
            correspondent="Sender",
            document_type="Invoice",
            topic="Test",
            extension=".txt",
        )
        assert result.endswith(".txt")


class TestMakeSuggestedFolder:
    def test_basic_structure(self):
        result = make_suggested_folder("Insurance", "HUK-COBURG", "Car Insurance")
        parts = result.split("/")
        assert parts[0] == "Insurance"
        assert "HUK-COBURG" in parts[1]

    def test_no_correspondent(self):
        result = make_suggested_folder("Taxes", None, "Annual return")
        assert "/" in result or result == "Taxes"

    def test_no_slashes_in_components(self):
        result = make_suggested_folder("Home/Garden", "My/Vendor", "Test/Topic")
        # Slashes should only appear as separators between components, not within
        for part in result.split("/"):
            assert "/" not in part


class TestResolveCollision:
    def test_no_collision(self, tmp_path):
        target = tmp_path / "document.pdf"
        result = resolve_collision(target)
        assert result == target

    def test_single_collision(self, tmp_path):
        target = tmp_path / "document.pdf"
        target.touch()
        result = resolve_collision(target)
        assert result != target
        assert "001" in result.name

    def test_multiple_collisions(self, tmp_path):
        target = tmp_path / "document.pdf"
        target.touch()
        (tmp_path / "document_001.pdf").touch()
        result = resolve_collision(target)
        assert "002" in result.name


class TestNormalizeUnicode:
    def test_nfc_normalization(self):
        # Composed form: single character ä (U+00E4)
        composed = "\u00e4"
        # Decomposed form: a + combining umlaut
        decomposed = "a\u0308"
        assert normalize_unicode(decomposed) == composed

    def test_plain_ascii_unchanged(self):
        assert normalize_unicode("Hello World") == "Hello World"
