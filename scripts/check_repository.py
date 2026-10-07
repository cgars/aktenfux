#!/usr/bin/env python3
"""Check repository documentation and tracked-artifact hygiene."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.render_architecture import (  # noqa: E402
    DEFAULT_SOURCES,
    DIAGRAM_PATTERN,
    discover_diagrams,
)


LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
REFERENCE_DEFINITION_PATTERN = re.compile(
    r"^\s{0,3}\[[^\]]+\]:\s*(?:<([^>]+)>|(\S+))", re.MULTILINE
)
MERMAID_FENCE_PATTERN = re.compile(
    r"(?P<fence>`{3,}|~{3,})mermaid[^\n]*\n"
    r".*?(?P=fence)[ \t]*(?:\n|$)",
    re.DOTALL,
)
TABLE_DIVIDER = re.compile(r":?-{3,}:?")
SQLITE_HEADER = b"SQLite format 3\x00"
SQLITE_SUFFIXES = (
    ".db",
    ".sqlite",
    ".sqlite3",
    ".db-journal",
    ".db-wal",
    ".db-shm",
    ".sqlite-journal",
    ".sqlite-wal",
    ".sqlite-shm",
    ".sqlite3-journal",
    ".sqlite3-wal",
    ".sqlite3-shm",
)
RUNTIME_ROOTS = {
    "_Inbox",
    "_Review",
    "_Imported",
    "_Error",
    "_Split",
    "_DryRun",
    "Archive",
}


@dataclass(frozen=True)
class GitEntry:
    path: str
    mode: str
    sha: str
    source: str


def markdown_files(root: Path, repository_paths: list[str]) -> list[Path]:
    """Return tracked and proposed Markdown, excluding Git-ignored environments."""
    return sorted(
        root / path
        for path in repository_paths
        if path.endswith(".md")
        and (root / path).is_file()
        and not (root / path).is_symlink()
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
        content = source.read_text(encoding="utf-8")
        for match in REFERENCE_DEFINITION_PATTERN.finditer(content):
            target = match.group(1) or match.group(2)
            parsed = urlsplit(target)
            if target.startswith("#") or parsed.scheme or parsed.netloc:
                continue
            relative = unquote(parsed.path)
            if relative and not (source.parent / relative).exists():
                line_number = content.count("\n", 0, match.start()) + 1
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
            if "|" not in header or "|" not in divider:
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
            while row < len(lines) and "|" in lines[row]:
                actual = len(_table_cells(lines[row]))
                if actual != expected:
                    errors.append(
                        f"{source.relative_to(REPO_ROOT)}:{row + 1}: "
                        f"table has {actual} columns; expected {expected}"
                    )
                row += 1
            index = row
    return errors


def _repository_paths(root: Path = REPO_ROOT) -> list[str]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [
        decoded
        for entry in result.stdout.split(b"\0")
        if entry
        if (decoded := entry.decode("utf-8"))
        and os.path.lexists(root / decoded)
    ]


def _parse_git_entries(raw: bytes, source: str) -> list[GitEntry]:
    entries: list[GitEntry] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", maxsplit=1)
        fields = metadata.decode("ascii").split()
        if source == "index":
            mode, sha, stage = fields
            if stage != "0":
                raise RuntimeError(f"unmerged Git index entry: {raw_path!r}")
        else:
            mode, object_type, sha = fields
            if object_type != "blob":
                continue
        entries.append(
            GitEntry(
                path=raw_path.decode("utf-8"),
                mode=mode,
                sha=sha,
                source=source,
            )
        )
    return entries


def _git_object_entries(root: Path = REPO_ROOT) -> list[GitEntry]:
    entries: list[GitEntry] = []
    if subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0:
        head = subprocess.run(
            ["git", "ls-tree", "-rz", "--full-tree", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
        )
        entries.extend(_parse_git_entries(head.stdout, "HEAD"))
    index = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    entries.extend(_parse_git_entries(index.stdout, "index"))
    return entries


def _git_blob_header(sha: str, root: Path) -> bytes:
    process = subprocess.Popen(
        ["git", "cat-file", "blob", sha],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    header = process.stdout.read(len(SQLITE_HEADER))
    process.stdout.close()
    process.wait()
    return header


def _is_sqlite_file(path: PurePosixPath, root: Path) -> bool:
    if path.name.lower().endswith(SQLITE_SUFFIXES):
        return True
    candidate = root / path
    if candidate.is_symlink() or not candidate.is_file():
        return False
    try:
        with candidate.open("rb") as handle:
            return handle.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def check_runtime_artifacts(
    paths: list[str], root: Path = REPO_ROOT
) -> list[str]:
    errors: list[str] = []
    for raw_path in paths:
        path = PurePosixPath(raw_path)
        forbidden = (
            (root / path).is_symlink()
            or path.name == "config.yaml"
            or path.name == ".env"
            or (path.name.startswith(".env.") and path.name != ".env.example")
            or path.suffix == ".pyc"
            or _is_sqlite_file(path, root)
            or path.name.lower().endswith(("-journal", "-wal", "-shm"))
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


def check_git_artifacts(
    entries: list[GitEntry] | None = None, root: Path = REPO_ROOT
) -> list[str]:
    errors: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    headers: dict[str, bytes] = {}
    for entry in entries if entries is not None else _git_object_entries(root):
        identity = (entry.path, entry.mode, entry.sha)
        if identity in seen:
            continue
        seen.add(identity)
        path = PurePosixPath(entry.path)
        if entry.sha not in headers:
            headers[entry.sha] = _git_blob_header(entry.sha, root)
        forbidden = (
            entry.mode == "120000"
            or path.name == "config.yaml"
            or path.name == ".env"
            or (path.name.startswith(".env.") and path.name != ".env.example")
            or path.suffix == ".pyc"
            or path.name.lower().endswith(SQLITE_SUFFIXES)
            or path.name.lower().endswith(("-journal", "-wal", "-shm"))
            or "__pycache__" in path.parts
            or "node_modules" in path.parts
            or (path.parts and path.parts[0] in RUNTIME_ROOTS)
            or (
                len(path.parts) >= 3
                and path.parts[:2] == ("build", "architecture")
                and path.suffix == ".svg"
            )
            or headers[entry.sha] == SQLITE_HEADER
        )
        if forbidden:
            errors.append(
                f"forbidden {entry.source} artifact: {entry.path} ({entry.sha[:12]})"
            )
    return errors


def check_named_diagrams(sources: list[Path] | None = None) -> list[str]:
    sources = sources or [REPO_ROOT / source for source in DEFAULT_SOURCES]
    try:
        discover_diagrams(sources)
    except SystemExit as exc:
        return [f"Mermaid source check failed: {exc}"]
    errors: list[str] = []
    for source in sources:
        content = source.read_text(encoding="utf-8")
        named_spans = [match.span() for match in DIAGRAM_PATTERN.finditer(content)]
        for fence in MERMAID_FENCE_PATTERN.finditer(content):
            if not any(start <= fence.start() and fence.end() <= end for start, end in named_spans):
                line_number = content.count("\n", 0, fence.start()) + 1
                errors.append(
                    f"{source.relative_to(REPO_ROOT)}:{line_number}: "
                    "Mermaid block lacks a <!-- diagram: name --> marker"
                )
    return errors


def main() -> int:
    repository_paths = _repository_paths()
    paths = markdown_files(REPO_ROOT, repository_paths)
    artifact_errors = check_runtime_artifacts(repository_paths)
    errors = [
        *artifact_errors,
        *check_git_artifacts(),
        *check_markdown_links(paths),
        *check_markdown_tables(paths),
        *check_named_diagrams(),
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
