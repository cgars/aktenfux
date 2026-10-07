#!/usr/bin/env python3
"""Check repository documentation and tracked-artifact hygiene."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.render_architecture import DEFAULT_SOURCES, discover_diagrams  # noqa: E402


LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
TABLE_DIVIDER = re.compile(r":?-{3,}:?")
IGNORED_PARTS = {".git", ".venv", "build", "node_modules"}
RUNTIME_ROOTS = {
    "_Inbox",
    "_Review",
    "_Imported",
    "_Error",
    "_Split",
    "_DryRun",
    "Archive",
}


def markdown_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.md")
        if not any(part in IGNORED_PARTS for part in path.relative_to(root).parts)
    )


def _link_destination(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("<") and ">" in raw:
        return raw[1 : raw.index(">")]
    return raw.split(maxsplit=1)[0]


def check_markdown_links(paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for source in paths:
        for line_number, line in enumerate(
            source.read_text(encoding="utf-8").splitlines(), start=1
        ):
            for raw_target in LINK_PATTERN.findall(line):
                target = _link_destination(raw_target)
                parsed = urlsplit(target)
                if not target or target.startswith("#") or parsed.scheme or parsed.netloc:
                    continue
                relative = unquote(parsed.path)
                if relative and not (source.parent / relative).exists():
                    errors.append(
                        f"{source.relative_to(REPO_ROOT)}:{line_number}: "
                        f"missing relative link target {relative!r}"
                    )
    return errors


def _table_cells(line: str) -> list[str]:
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    in_code = False
    for char in line.strip().strip("|"):
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
            current.append(char)
        elif char == "`":
            in_code = not in_code
            current.append(char)
        elif char == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    return cells


def check_markdown_tables(paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for source in paths:
        lines = source.read_text(encoding="utf-8").splitlines()
        index = 0
        while index + 1 < len(lines):
            header = lines[index]
            divider = lines[index + 1]
            if not (header.strip().startswith("|") and divider.strip().startswith("|")):
                index += 1
                continue
            header_cells = _table_cells(header)
            divider_cells = _table_cells(divider)
            if not divider_cells or not all(
                TABLE_DIVIDER.fullmatch(cell.replace(" ", ""))
                for cell in divider_cells
            ):
                index += 1
                continue
            expected = len(header_cells)
            if len(divider_cells) != expected:
                errors.append(
                    f"{source.relative_to(REPO_ROOT)}:{index + 2}: "
                    f"table has {len(divider_cells)} columns; expected {expected}"
                )
            row = index + 2
            while row < len(lines) and lines[row].strip().startswith("|"):
                actual = len(_table_cells(lines[row]))
                if actual != expected:
                    errors.append(
                        f"{source.relative_to(REPO_ROOT)}:{row + 1}: "
                        f"table has {actual} columns; expected {expected}"
                    )
                row += 1
            index = row
    return errors


def _repository_paths() -> list[str]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return [entry.decode("utf-8") for entry in result.stdout.split(b"\0") if entry]


def check_runtime_artifacts(paths: list[str]) -> list[str]:
    errors: list[str] = []
    for raw_path in paths:
        path = PurePosixPath(raw_path)
        forbidden = (
            path.name == "config.yaml"
            or path.name == ".env"
            or (path.name.startswith(".env.") and path.name != ".env.example")
            or path.suffix == ".pyc"
            or path.name.endswith((".db", ".db-journal"))
            or "__pycache__" in path.parts
            or "node_modules" in path.parts
            or (path.parts and path.parts[0] in RUNTIME_ROOTS)
            or (
                len(path.parts) >= 3
                and path.parts[:2] == ("build", "architecture")
                and path.suffix == ".svg"
            )
        )
        if forbidden:
            errors.append(f"tracked or unignored runtime artifact: {raw_path}")
    return errors


def check_named_diagrams() -> list[str]:
    try:
        discover_diagrams([REPO_ROOT / source for source in DEFAULT_SOURCES])
    except SystemExit as exc:
        return [f"Mermaid source check failed: {exc}"]
    return []


def main() -> int:
    paths = markdown_files(REPO_ROOT)
    errors = [
        *check_markdown_links(paths),
        *check_markdown_tables(paths),
        *check_named_diagrams(),
        *check_runtime_artifacts(_repository_paths()),
    ]
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(
        f"repository checks passed: {len(paths)} Markdown files, "
        f"{len(discover_diagrams([REPO_ROOT / source for source in DEFAULT_SOURCES]))} diagrams"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
