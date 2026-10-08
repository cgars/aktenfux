#!/usr/bin/env python3
"""Check repository documentation and tracked-artifact hygiene."""
from __future__ import annotations

import os
import posixpath
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.render_architecture import (  # noqa: E402
    DEFAULT_SOURCES,
    discover_diagrams,
    discover_diagrams_from_texts,
    find_mermaid_blocks,
    named_mermaid_blocks,
)


LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
REFERENCE_DEFINITION_PATTERN = re.compile(
    r"^\s{0,3}\[[^\]]+\]:\s*(?:<([^>]+)>|(\S+))", re.MULTILINE
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


def _check_markdown_links_text(
    label: str,
    content: str,
    target_exists: Callable[[str], bool],
) -> list[str]:
    errors: list[str] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        for raw_target in LINK_PATTERN.findall(line):
            target = _link_destination(raw_target)
            parsed = urlsplit(target)
            if not target or target.startswith("#") or parsed.scheme or parsed.netloc:
                continue
            relative = unquote(parsed.path)
            if relative and not target_exists(relative):
                errors.append(
                    f"{label}:{line_number}: "
                    f"missing relative link target {relative!r}"
                )
    for match in REFERENCE_DEFINITION_PATTERN.finditer(content):
        target = match.group(1) or match.group(2)
        parsed = urlsplit(target)
        if target.startswith("#") or parsed.scheme or parsed.netloc:
            continue
        relative = unquote(parsed.path)
        if relative and not target_exists(relative):
            line_number = content.count("\n", 0, match.start()) + 1
            errors.append(
                f"{label}:{line_number}: missing relative link target {relative!r}"
            )
    return errors


def check_markdown_links(paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for source in paths:
        content = source.read_text(encoding="utf-8")
        errors.extend(
            _check_markdown_links_text(
                str(source.relative_to(REPO_ROOT)),
                content,
                lambda relative, parent=source.parent: (parent / relative).exists(),
            )
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


def _check_markdown_tables_text(label: str, content: str) -> list[str]:
    errors: list[str] = []
    lines = content.splitlines()
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
                f"{label}:{index + 2}: "
                f"table has {len(divider_cells)} columns; expected {expected}"
            )
        row = index + 2
        while row < len(lines) and "|" in lines[row]:
            actual = len(_table_cells(lines[row]))
            if actual != expected:
                errors.append(
                    f"{label}:{row + 1}: "
                    f"table has {actual} columns; expected {expected}"
                )
            row += 1
        index = row
    return errors


def check_markdown_tables(paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for source in paths:
        errors.extend(
            _check_markdown_tables_text(
                str(source.relative_to(REPO_ROOT)),
                source.read_text(encoding="utf-8"),
            )
        )
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


def _git_blob(sha: str, root: Path) -> bytes:
    return subprocess.run(
        ["git", "cat-file", "blob", sha],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout


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


def _is_forbidden_artifact_path(path: PurePosixPath) -> bool:
    return (
        path.name == "config.yaml"
        or path.name == ".env"
        or (path.name.startswith(".env.") and path.name != ".env.example")
        or path.suffix == ".pyc"
        or path.name.lower().endswith(SQLITE_SUFFIXES)
        or path.name.lower().endswith(("-journal", "-wal", "-shm"))
        or "__pycache__" in path.parts
        or "node_modules" in path.parts
        or any(part in RUNTIME_ROOTS for part in path.parts)
        or (
            len(path.parts) >= 3
            and path.parts[:2] == ("build", "architecture")
            and path.suffix == ".svg"
        )
    )


def check_runtime_artifacts(
    paths: list[str], root: Path = REPO_ROOT
) -> list[str]:
    errors: list[str] = []
    for raw_path in paths:
        path = PurePosixPath(raw_path)
        forbidden = (
            (root / path).is_symlink()
            or _is_forbidden_artifact_path(path)
            or _is_sqlite_file(path, root)
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
        forbidden = entry.mode == "120000" or _is_forbidden_artifact_path(path)
        if not forbidden:
            if entry.sha not in headers:
                headers[entry.sha] = _git_blob_header(entry.sha, root)
            forbidden = headers[entry.sha] == SQLITE_HEADER
        if forbidden:
            errors.append(
                f"forbidden {entry.source} artifact: {entry.path} ({entry.sha[:12]})"
            )
    return errors


def _snapshot_target_exists(
    source: PurePosixPath, relative: str, paths: set[str]
) -> bool:
    target = posixpath.normpath(str(source.parent / relative))
    if target == ".." or target.startswith("../") or target.startswith("/"):
        return False
    prefix = target.rstrip("/") + "/"
    return target in paths or any(path.startswith(prefix) for path in paths)


def _check_named_diagram_texts(sources: list[tuple[str, str]]) -> list[str]:
    try:
        discover_diagrams_from_texts(sources)
    except SystemExit as exc:
        return [f"Mermaid source check failed: {exc}"]
    errors: list[str] = []
    for label, content in sources:
        named_blocks = {block for _, block in named_mermaid_blocks(content)}
        for block in find_mermaid_blocks(content):
            line_number = content.count("\n", 0, block.start) + 1
            if not block.closed:
                errors.append(f"{label}:{line_number}: unclosed Mermaid fence")
            if block not in named_blocks:
                errors.append(
                    f"{label}:{line_number}: "
                    "Mermaid block lacks a <!-- diagram: name --> marker"
                )
    return errors


def check_git_markdown(
    entries: list[GitEntry] | None = None, root: Path = REPO_ROOT
) -> list[str]:
    """Check Markdown exactly as stored in committed and staged Git snapshots."""
    entries = entries if entries is not None else _git_object_entries(root)
    errors: list[str] = []
    blob_cache: dict[str, bytes] = {}
    default_sources = {source.as_posix() for source in DEFAULT_SOURCES}
    for snapshot in ("HEAD", "index"):
        snapshot_entries = [entry for entry in entries if entry.source == snapshot]
        paths = {entry.path for entry in snapshot_entries}
        markdown: list[tuple[GitEntry, str]] = []
        for entry in snapshot_entries:
            if not entry.path.endswith(".md") or entry.mode == "120000":
                continue
            if entry.sha not in blob_cache:
                blob_cache[entry.sha] = _git_blob(entry.sha, root)
            try:
                content = blob_cache[entry.sha].decode("utf-8")
            except UnicodeDecodeError:
                errors.append(f"{snapshot} {entry.path}: Markdown is not UTF-8")
                continue
            markdown.append((entry, content))
            label = f"{snapshot} {entry.path}"
            source = PurePosixPath(entry.path)
            errors.extend(
                _check_markdown_links_text(
                    label,
                    content,
                    lambda relative, source=source, paths=paths: _snapshot_target_exists(
                        source, relative, paths
                    ),
                )
            )
            errors.extend(_check_markdown_tables_text(label, content))
        diagram_sources = [
            (f"{snapshot} {entry.path}", content)
            for entry, content in markdown
            if entry.path in default_sources
        ]
        if diagram_sources:
            errors.extend(_check_named_diagram_texts(diagram_sources))
    return errors


def check_named_diagrams(sources: list[Path] | None = None) -> list[str]:
    sources = sources or [REPO_ROOT / source for source in DEFAULT_SOURCES]
    return _check_named_diagram_texts(
        [
            (str(source.relative_to(REPO_ROOT)), source.read_text(encoding="utf-8"))
            for source in sources
        ]
    )


def main() -> int:
    repository_paths = _repository_paths()
    paths = markdown_files(REPO_ROOT, repository_paths)
    artifact_errors = check_runtime_artifacts(repository_paths)
    git_entries = _git_object_entries()
    errors = [
        *artifact_errors,
        *check_git_artifacts(git_entries),
        *check_git_markdown(git_entries),
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
