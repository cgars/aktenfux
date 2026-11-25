"""Synthetic smoke tests for the standalone sidecar-search helper."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "open_json_pdfs.py"
SPEC = importlib.util.spec_from_file_location("open_json_pdfs", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SidecarSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.nested = self.root / "Archive" / "Letters"
        self.nested.mkdir(parents=True)
        (self.nested / "invoice.pdf").write_bytes(b"%PDF-1.4\n")
        (self.nested / "invoice.json").write_text(
            json.dumps({"topic": "Freiburg", "summary": "Telekom Rechnung"}),
            encoding="utf-8",
        )
        (self.nested / "other.pdf").write_bytes(b"%PDF-1.4\n")

    def search(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.root), "--paths", *args],
            check=False,
            capture_output=True,
            text=True,
        )

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.root), *args],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_recursive_and_case_insensitive(self) -> None:
        result = self.search("FREIBURG", "rechnung")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.nested / "invoice.pdf"))

    def test_and_semantics(self) -> None:
        result = self.search("Freiburg", "Hamburg")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(result.stdout.strip())

    def test_ignores_missing_sidecars(self) -> None:
        result = self.search("other")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(result.stdout.strip())

    def test_does_not_follow_pdf_symlink(self) -> None:
        target = self.nested / "invoice.pdf"
        link = self.root / "linked.pdf"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        result = self.search("Telekom")
        self.assertNotIn(str(link), result.stdout)

    def test_decoded_recursive_search_matches_quotes_backslashes_and_keys(self) -> None:
        target = self.nested / "escaped.pdf"
        sidecar = self.nested / "escaped.json"
        target.write_bytes(b"%PDF-1.4\n")
        sidecar.write_text(
            json.dumps(
                {
                    "topic": "Ignored",
                    "meta": {
                        "quoted_text": 'A literal "quote"',
                        "windows_path": r"C:\Users\Alice\Docs",
                    },
                }
            ),
            encoding="utf-8",
        )
        result = self.search("quoted_text", '"quote"', r"C:\Users\Alice\Docs")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(target), result.stdout.strip())

    def test_human_output_escapes_terminal_control_sequences(self) -> None:
        evil_pdf = self.nested / "evil-\x1b[31m.pdf"
        evil_pdf.write_bytes(b"%PDF-1.4\n")
        (self.nested / "evil-\x1b[31m.json").write_text(
            json.dumps({"topic": "alert-\x1b[2J"}),
            encoding="utf-8",
        )

        listed = self.run_script("--list", "alert")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertNotIn("\x1b", listed.stdout)
        self.assertIn("\\x1b", listed.stdout)

        machine = self.search("alert")
        self.assertEqual(machine.returncode, 0, machine.stderr)
        self.assertIn("\x1b", machine.stdout)

    def test_fzf_input_escapes_terminal_control_sequences(self) -> None:
        matches = [
            (
                self.nested / "name-\x1b[31m.pdf",
                {"topic": "topic-\x1b[2J", "document_date": "2026-11-15"},
            ),
            (
                self.nested / "safe.pdf",
                {"topic": "safe", "document_date": "2026-11-15"},
            ),
        ]
        with (
            mock.patch.object(MODULE.shutil, "which", return_value="/usr/bin/fzf"),
            mock.patch.object(
                MODULE.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(args=["fzf"], returncode=1, stdout=""),
            ) as mocked_run,
        ):
            selected = MODULE.select_pdf(matches)
        self.assertIsNone(selected)
        submitted = mocked_run.call_args.kwargs["input"]
        self.assertNotIn("\x1b", submitted)
        self.assertIn("\\x1b", submitted)

    def test_invalid_json_warning_redacts_full_paths_by_default(self) -> None:
        bad_pdf = self.nested / "bad.pdf"
        bad_sidecar = self.nested / "bad.json"
        bad_pdf.write_bytes(b"%PDF-1.4\n")
        bad_sidecar.write_text('{"topic": "broken"', encoding="utf-8")

        result = self.run_script("--list", "broken")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("invalid JSON", result.stderr)
        self.assertIn(bad_sidecar.name, result.stderr)
        self.assertNotIn(str(bad_sidecar), result.stderr)
        self.assertNotIn(str(self.root), result.stderr)

    def test_read_failure_warning_redacts_full_paths_by_default(self) -> None:
        unreadable_pdf = self.nested / "unreadable.pdf"
        unreadable_sidecar = self.nested / "unreadable.json"
        unreadable_pdf.write_bytes(b"%PDF-1.4\n")
        unreadable_sidecar.write_text(json.dumps({"topic": "secret"}), encoding="utf-8")
        with mock.patch.object(
            Path,
            "read_text",
            side_effect=OSError(f"boom at {self.root / 'hidden' / 'path.json'}"),
        ):
            stderr_default = io.StringIO()
            with contextlib.redirect_stderr(stderr_default):
                text, _ = MODULE.read_sidecar(unreadable_pdf, show_sensitive_paths=False)
        self.assertEqual(text, "")
        self.assertIn("cannot read sidecar", stderr_default.getvalue())
        self.assertIn(unreadable_sidecar.name, stderr_default.getvalue())
        self.assertNotIn(str(unreadable_sidecar), stderr_default.getvalue())
        self.assertNotIn(str(self.root), stderr_default.getvalue())

    def test_pdftotext_warning_redacts_and_opt_in_reveals_paths(self) -> None:
        pdf = self.nested / "invoice.pdf"
        sensitive = str(self.root / "private" / "top-secret.pdf")
        failure = subprocess.CompletedProcess(
            args=["pdftotext"],
            returncode=1,
            stdout=b"",
            stderr=f"failed near {sensitive}".encode(),
        )
        with mock.patch.object(MODULE.subprocess, "run", return_value=failure):
            stderr_default = io.StringIO()
            with contextlib.redirect_stderr(stderr_default):
                extracted = MODULE.extract_pdf_text(pdf, show_sensitive_paths=False)
            self.assertEqual(extracted, "")
            self.assertNotIn(str(pdf), stderr_default.getvalue())
            self.assertNotIn(sensitive, stderr_default.getvalue())

            stderr_sensitive = io.StringIO()
            with contextlib.redirect_stderr(stderr_sensitive):
                MODULE.extract_pdf_text(pdf, show_sensitive_paths=True)
            self.assertIn(str(pdf), stderr_sensitive.getvalue())
            self.assertIn(sensitive, stderr_sensitive.getvalue())


if __name__ == "__main__":
    unittest.main()
