"""Synthetic smoke tests for the standalone sidecar-search helper."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "open_json_pdfs.py"


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


if __name__ == "__main__":
    unittest.main()
