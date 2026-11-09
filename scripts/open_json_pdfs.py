#!/usr/bin/env python3
"""Search Aktenfux JSON sidecars and optionally the text inside their PDFs.

Examples:
    python3 scripts/open_json_pdfs.py ~/Documents/Aktenfux 'Telekom' 'Rechnung'
    python3 scripts/open_json_pdfs.py ~/Documents/Aktenfux --pdf-text 'Vertragsnummer'
    python3 scripts/open_json_pdfs.py ~/Documents/Aktenfux --list 'Freiburg'

Sidecars normally live next to the PDF: document.pdf + document.json.
Also supports document.pdf.json. No Aktenfux installation is required.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find PDFs via Aktenfux sidecars (AND matching, case-insensitive)."
    )
    parser.add_argument("directory", type=Path, help="Directory to search recursively")
    parser.add_argument("terms", nargs="+", metavar="TERM", help="Literal search words or phrases")
    parser.add_argument(
        "--pdf-text", "--fulltext", dest="pdf_text", action="store_true",
        help="Also search embedded/OCR text in PDFs (needs pdftotext)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="Only list results; do not open an interactive picker",
    )
    parser.add_argument(
        "--paths", action="store_true",
        help="Output PDF paths only (implies --list; useful for scripts)",
    )
    return parser.parse_args()


def pdf_files(root: Path):
    # os.walk does not recurse into linked directories, avoiding loops.
    for folder, dirs, files in os.walk(root):
        dirs.sort(key=str.casefold)
        for name in sorted(files, key=str.casefold):
            if name.lower().endswith(".pdf"):
                candidate = Path(folder) / name
                if candidate.is_file() and not candidate.is_symlink():
                    yield candidate


def read_sidecar(pdf: Path) -> tuple[str, dict]:
    # Aktenfux writes pdf.with_suffix('.json'); older/other sidecars
    # sometimes use the PDF's complete filename followed by '.json'.
    candidates = [pdf.with_suffix(".json"), pdf.with_name(pdf.name + ".json")]
    for sidecar in candidates:
        if not sidecar.is_file() or sidecar.is_symlink():
            continue
        try:
            raw = sidecar.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            print(f"Warning: cannot read {sidecar}: {exc}", file=sys.stderr)
            continue
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                # Decode JSON escapes, so search 'Müller' also matches '\\u00fcller'.
                return json.dumps(data, ensure_ascii=False), data
        except json.JSONDecodeError:
            print(f"Warning: invalid JSON in {sidecar}; searching raw text", file=sys.stderr)
        return raw, {}
    return "", {}


def extract_pdf_text(pdf: Path) -> str:
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", str(pdf), "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            msg = proc.stderr.decode("utf-8", errors="replace").strip()
            print(f"Warning: pdftotext failed for {pdf}: {msg}", file=sys.stderr)
            return ""
        return proc.stdout.decode("utf-8", errors="replace")
    except OSError as exc:
        print(f"Warning: could not process {pdf}: {exc}", file=sys.stderr)
        return ""


def short(value: object, limit: int = 90) -> str:
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    return value[: limit - 1] + "…" if len(value) > limit else value


def title(meta: dict) -> str:
    parts = [short(meta.get("document_date"), 12), short(meta.get("topic"), 65)]
    if not parts[-1]:
        parts[-1] = short(meta.get("summary_short"), 65)
    if not parts[-1]:
        parts[-1] = short(meta.get("correspondent"), 65)
    return " · ".join(part for part in parts if part)


def in_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text().casefold()
    except OSError:
        return False


def open_pdf(pdf: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(pdf))  # type: ignore[attr-defined]
    elif in_wsl() and shutil.which("explorer.exe") and shutil.which("wslpath"):
        wslpath = subprocess.run(
            ["wslpath", "-w", str(pdf)], text=True, capture_output=True, check=True
        ).stdout.strip()
        subprocess.Popen(["explorer.exe", wslpath], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        if not shutil.which(opener):
            raise OSError(f"No PDF viewer launcher available ({opener})")
        subprocess.Popen([opener, str(pdf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def select_pdf(matches: list[tuple[Path, dict]]) -> Path | None:
    if len(matches) == 1:
        return matches[0][0]
    if shutil.which("fzf"):
        choices = {
            f"{i:3}. {pdf.name}  |  {title(meta)}  |  {pdf.parent}": pdf
            for i, (pdf, meta) in enumerate(matches, start=1)
        }
        try:
            # fzf draws its interactive UI on stderr and reads keystrokes
            # from the controlling terminal.  Never capture stderr here:
            # capture_output=True hides the UI (and can break interaction).
            # Capture only stdout, which contains the chosen entry.
            result = subprocess.run(
                ["fzf", "--prompt", "PDF > ", "--height", "80%", "--reverse"],
                input="\n".join(choices) + "\n",
                text=True,
                stdout=subprocess.PIPE,
                stderr=None,
                check=False,
            )
            return choices.get(result.stdout.rstrip("\r\n")) if result.returncode == 0 else None
        except OSError:
            pass
    try:
        selection = input("PDF number to open (Enter = cancel): ").strip()
        if not selection:
            return None
        number = int(selection)
        if 1 <= number <= len(matches):
            return matches[number - 1][0]
    except (EOFError, ValueError, KeyboardInterrupt):
        pass
    return None


def main() -> int:
    args = arguments()
    root = args.directory.expanduser().resolve()
    if not root.is_dir():
        print(f"Directory not found: {root}", file=sys.stderr)
        return 2
    if args.pdf_text and not shutil.which("pdftotext"):
        print("--pdf-text requires pdftotext (Debian/Ubuntu: sudo apt install poppler-utils)", file=sys.stderr)
        return 2

    terms = [term.casefold() for term in args.terms]
    matches: list[tuple[Path, dict]] = []
    count = 0
    for pdf in pdf_files(root):
        count += 1
        sidecar_text, meta = read_sidecar(pdf)
        # Without --pdf-text, PDFs without a sidecar are not searchable.
        if not sidecar_text and not args.pdf_text:
            continue
        sidecar_folded = sidecar_text.casefold()
        missing = [term for term in terms if term not in sidecar_folded]
        if missing and args.pdf_text:
            pdf_folded = extract_pdf_text(pdf).casefold()
            missing = [term for term in missing if term not in pdf_folded]
        if not missing:
            matches.append((pdf, meta))

    if args.paths:
        for pdf, _ in matches:
            print(pdf)
        return 0

    if not matches:
        print(f"No matching PDFs found (scanned {count}).")
        return 0

    print(f"Found {len(matches)} matching PDF(s) (scanned {count}):\n")
    for i, (pdf, meta) in enumerate(matches, 1):
        extra = title(meta)
        print(f"{i:3}. {pdf}")
        if extra:
            print(f"     {extra}")
    print()

    if args.list or not sys.stdin.isatty():
        return 0
    choice = select_pdf(matches)
    if choice:
        try:
            open_pdf(choice)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"Could not open {choice}: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
