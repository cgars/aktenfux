"""Read-only inventory and consistency checks for archived PDF/JSON pairs."""
from __future__ import annotations

import os
import re
import stat
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO
from unicodedata import normalize

from pydantic import ValidationError

from aktenfux.config import AktenfuxConfig
from aktenfux.schema import SidecarDocument
from aktenfux.storage import sha256_stream, sidecar_path_for

_MAX_SIDECAR_BYTES = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")


class ArchiveCheckError(ValueError):
    """The configured archive cannot be identified safely."""


class FindingKind(StrEnum):
    DIRECTORY_UNAVAILABLE = "directory_unavailable"
    UNSAFE_ENTRY = "unsafe_entry"
    AMBIGUOUS_PAIR = "ambiguous_pair"
    MISSING_SIDECAR = "missing_sidecar"
    MISSING_PDF = "missing_pdf"
    NONCANONICAL_PAIR = "noncanonical_pair"
    INVALID_SIDECAR = "invalid_sidecar"
    SIDECAR_TOO_LARGE = "sidecar_too_large"
    UNREADABLE_OR_CHANGED = "unreadable_or_changed"
    PATH_MISMATCH = "path_mismatch"
    STATUS_MISMATCH = "status_mismatch"
    HASH_MISMATCH = "hash_mismatch"


@dataclass(frozen=True)
class ArchiveFinding:
    kind: FindingKind
    relative_path: Path


@dataclass(frozen=True)
class ArchiveCheckResult:
    pdf_candidates: int
    verified_pairs: int
    findings: tuple[ArchiveFinding, ...]


class _UnsafeEntry(OSError):
    """An archive entry is unsafe to open."""


def _name_key(name: str) -> str:
    return normalize("NFC", name).casefold()


def _paths_overlap(first: Path, second: Path) -> bool:
    first_parts = tuple(map(_name_key, first.parts))
    second_parts = tuple(map(_name_key, second.parts))
    common_length = min(len(first_parts), len(second_parts))
    return first_parts[:common_length] == second_parts[:common_length]


def _is_link(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _archive_root(config: AktenfuxConfig) -> Path:
    name = config.archive_dir
    if not isinstance(name, str) or name in {"", ".", ".."} or any(
        character in name for character in "/\\:\0"
    ):
        raise ArchiveCheckError("Archive must be a direct child of base_dir.")
    root = config.archive_path
    try:
        metadata = root.lstat()
        if _is_link(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ArchiveCheckError("Archive must be a regular directory without links.")
        for other in (
            config.inbox_path,
            config.review_path,
            config.imported_path,
            config.error_path,
            config.split_path,
            config.dry_run_path,
        ):
            if (
                _paths_overlap(root, other)
                or _paths_overlap(root, other.resolve())
                or (other.exists() and root.samefile(other))
            ):
                raise ArchiveCheckError("Archive overlaps another lifecycle directory.")
    except ArchiveCheckError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ArchiveCheckError("Could not validate the configured archive directory.") from exc
    return root


def _check_directory(root: Path, directory: Path) -> None:
    """Check all path components within the configured archive."""
    for path in (directory, *directory.parents):
        metadata = path.lstat()
        if _is_link(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise _UnsafeEntry
        if path == root:
            return
    raise _UnsafeEntry


def _file_snapshot(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if _is_link(metadata) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise _UnsafeEntry
    return metadata


def _same_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
    return os.path.samestat(first, second) and (
        first.st_size, first.st_mtime_ns, first.st_ctime_ns
    ) == (second.st_size, second.st_mtime_ns, second.st_ctime_ns)


@contextmanager
def _open_archive_file(root: Path, path: Path) -> Iterator[BinaryIO]:
    _check_directory(root, path.parent)
    before = _file_snapshot(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise _UnsafeEntry
        _check_directory(root, path.parent)
        if not _same_snapshot(before, opened) or not _same_snapshot(opened, _file_snapshot(path)):
            raise OSError("Archive entry changed at open.")
        yield stream
        _check_directory(root, path.parent)
        if not _same_snapshot(opened, _file_snapshot(path)):
            raise OSError("Archive entry changed during read.")


def _inspect_pair(root: Path, pdf: Path, sidecar: Path) -> list[ArchiveFinding]:
    pdf_relative = pdf.relative_to(root)
    reading = sidecar
    try:
        with _open_archive_file(root, sidecar) as stream:
            payload = stream.read(_MAX_SIDECAR_BYTES + 1)
        if len(payload) > _MAX_SIDECAR_BYTES:
            return [ArchiveFinding(FindingKind.SIDECAR_TOO_LARGE, sidecar.relative_to(root))]
        document = SidecarDocument.model_validate_json(payload)
        if not document.id.strip() or not _SHA256.fullmatch(document.sha256):
            return [ArchiveFinding(FindingKind.INVALID_SIDECAR, sidecar.relative_to(root))]
        reading = pdf
        with _open_archive_file(root, pdf) as stream:
            digest = sha256_stream(stream)
    except ValidationError:
        return [ArchiveFinding(FindingKind.INVALID_SIDECAR, sidecar.relative_to(root))]
    except _UnsafeEntry:
        return [ArchiveFinding(FindingKind.UNSAFE_ENTRY, reading.relative_to(root))]
    except OSError:
        return [ArchiveFinding(FindingKind.UNREADABLE_OR_CHANGED, reading.relative_to(root))]

    findings = []
    # Compare recorded locations lexically. The sidecar never supplies a read path.
    if Path(document.current_path) != pdf:
        findings.append(ArchiveFinding(FindingKind.PATH_MISMATCH, pdf_relative))
    if document.status != "approved":
        findings.append(ArchiveFinding(FindingKind.STATUS_MISMATCH, pdf_relative))
    if document.sha256.lower() != digest:
        findings.append(ArchiveFinding(FindingKind.HASH_MISMATCH, pdf_relative))
    return findings


def check_archive(config: AktenfuxConfig) -> ArchiveCheckResult:
    """Inspect nested archive pairs and distinguish findings from an empty archive."""
    root = _archive_root(config)
    directories = [root]
    findings: list[ArchiveFinding] = []
    pdf_candidates = verified_pairs = 0
    while directories:
        directory = directories.pop()
        try:
            _check_directory(root, directory)
            entries = sorted(directory.iterdir())
            _check_directory(root, directory)
        except _UnsafeEntry:
            findings.append(ArchiveFinding(FindingKind.UNSAFE_ENTRY, directory.relative_to(root)))
            continue
        except OSError:
            findings.append(ArchiveFinding(FindingKind.DIRECTORY_UNAVAILABLE, directory.relative_to(root)))
            continue

        groups: dict[str, list[Path]] = defaultdict(list)
        for entry in entries:
            suffix = entry.suffix.lower()
            try:
                metadata = entry.lstat()
            except OSError:
                findings.append(ArchiveFinding(FindingKind.UNREADABLE_OR_CHANGED, entry.relative_to(root)))
                continue
            if suffix == ".pdf" and not stat.S_ISDIR(metadata.st_mode):
                pdf_candidates += 1
            if _is_link(metadata):
                findings.append(ArchiveFinding(FindingKind.UNSAFE_ENTRY, entry.relative_to(root)))
            elif stat.S_ISDIR(metadata.st_mode):
                directories.append(entry)
            elif suffix in {".json", ".pdf"}:
                if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                    groups[_name_key(entry.stem)].append(entry)
                else:
                    findings.append(ArchiveFinding(FindingKind.UNSAFE_ENTRY, entry.relative_to(root)))

        for group in groups.values():
            pdfs = [path for path in group if path.suffix.lower() == ".pdf"]
            sidecars = [path for path in group if path.suffix.lower() == ".json"]
            if len(pdfs) > 1 or len(sidecars) > 1:
                findings.extend(
                    ArchiveFinding(FindingKind.AMBIGUOUS_PAIR, path.relative_to(root))
                    for path in group
                )
            elif not pdfs:
                findings.append(ArchiveFinding(FindingKind.MISSING_PDF, sidecars[0].relative_to(root)))
            elif not sidecars:
                findings.append(ArchiveFinding(FindingKind.MISSING_SIDECAR, pdfs[0].relative_to(root)))
            else:
                pdf, sidecar = pdfs[0], sidecars[0]
                if pdf.suffix != ".pdf" or sidecar.name != sidecar_path_for(pdf).name:
                    findings.append(ArchiveFinding(FindingKind.NONCANONICAL_PAIR, pdf.relative_to(root)))
                    continue
                pair_findings = _inspect_pair(root, pdf, sidecar)
                findings.extend(pair_findings)
                verified_pairs += not pair_findings

    return ArchiveCheckResult(pdf_candidates, verified_pairs, tuple(findings))
