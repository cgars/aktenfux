#!/usr/bin/env python3
"""Render named Mermaid blocks from maintained architecture documents."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

DIAGRAM_MARKER_PATTERN = re.compile(
    r"<!--\s*diagram:\s*(?P<name>[a-z0-9-]+)\s*-->\s*$"
)
FENCE_OPEN_PATTERN = re.compile(
    r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>[^\r\n]*)\r?\n?$"
)

DEFAULT_SOURCES = [Path("docs/architecture.md"), Path("docs/threat-model.md")]


@dataclass(frozen=True)
class MermaidBlock:
    """One Mermaid fenced block and its exact source span."""

    start: int
    end: int
    body: str
    closed: bool


def find_mermaid_blocks(content: str) -> list[MermaidBlock]:
    """Find CommonMark Mermaid fences, including longer valid closing fences."""
    lines = content.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)

    blocks: list[MermaidBlock] = []
    index = 0
    while index < len(lines):
        opener = FENCE_OPEN_PATTERN.fullmatch(lines[index])
        if not opener:
            index += 1
            continue
        fence = opener.group("fence")
        info = opener.group("info").strip()
        fence_char = fence[0]
        closing_pattern = re.compile(
            rf"^ {{0,3}}{re.escape(fence_char)}{{{len(fence)},}}[ \t]*\r?\n?$"
        )
        closing_index = index + 1
        while closing_index < len(lines) and not closing_pattern.fullmatch(
            lines[closing_index]
        ):
            closing_index += 1

        is_mermaid = bool(re.match(r"^mermaid(?:\s|$)", info))
        body_start = offsets[index] + len(lines[index])
        if closing_index < len(lines):
            if is_mermaid:
                blocks.append(
                    MermaidBlock(
                        start=offsets[index],
                        end=offsets[closing_index] + len(lines[closing_index]),
                        body=content[body_start : offsets[closing_index]],
                        closed=True,
                    )
                )
            index = closing_index + 1
        else:
            if is_mermaid:
                blocks.append(
                    MermaidBlock(
                        start=offsets[index],
                        end=len(content),
                        body=content[body_start:],
                        closed=False,
                    )
                )
            break
    return blocks


def named_mermaid_blocks(content: str) -> list[tuple[str, MermaidBlock]]:
    """Return Mermaid blocks with an immediately preceding diagram marker."""
    named: list[tuple[str, MermaidBlock]] = []
    for block in find_mermaid_blocks(content):
        marker = DIAGRAM_MARKER_PATTERN.search(content[: block.start])
        if marker:
            named.append((marker.group("name"), block))
    return named


def discover_diagrams_from_texts(
    sources: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Discover named diagrams from labeled in-memory source snapshots."""
    diagrams: list[tuple[str, str]] = []
    for source, content in sources:
        for name, block in named_mermaid_blocks(content):
            if not block.closed:
                raise SystemExit(f"Unclosed Mermaid fence in {source}")
            diagrams.append((name, block.body))

    if not diagrams:
        raise SystemExit("No named Mermaid diagrams found")

    names = [name for name, _ in diagrams]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise SystemExit("Diagram names must be unique: {}".format(", ".join(duplicates)))
    return diagrams


def discover_diagrams(sources: list[Path]) -> list[tuple[str, str]]:
    return discover_diagrams_from_texts(
        [(str(source), source.read_text(encoding="utf-8")) for source in sources]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="*", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--output-dir", type=Path, default=Path("build/architecture"))
    parser.add_argument(
        "--puppeteer-no-sandbox",
        action="store_true",
        help="Disable the Chromium sandbox for restricted CI runners only.",
    )
    args = parser.parse_args()

    diagrams = discover_diagrams(args.sources)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    binary_name = "mmdc.cmd" if os.name == "nt" else "mmdc"
    local_mmdc = Path("node_modules") / ".bin" / binary_name
    if not local_mmdc.is_file():
        raise SystemExit(
            f"Locked Mermaid CLI not found at {local_mmdc}. Run 'npm ci' first."
        )
    command = [str(local_mmdc.resolve())]

    with tempfile.TemporaryDirectory(prefix="architecture-") as temp_dir:
        temp_path = Path(temp_dir)
        browser_args: list[str] = []
        if args.puppeteer_no_sandbox:
            puppeteer_config = temp_path / "puppeteer.json"
            puppeteer_config.write_text(
                json.dumps(
                    {"args": ["--no-sandbox", "--disable-setuid-sandbox"]},
                    indent=2,
                ),
                encoding="utf-8",
            )
            browser_args = ["-p", str(puppeteer_config)]

        for name, diagram in diagrams:
            input_path = temp_path / f"{name}.mmd"
            output_path = args.output_dir / f"{name}.svg"
            input_path.write_text(diagram.strip() + "\n", encoding="utf-8")
            subprocess.run(
                command
                + browser_args
                + ["-i", str(input_path), "-o", str(output_path), "-b", "transparent"],
                check=True,
            )
            print(f"generated {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
