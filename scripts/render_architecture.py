#!/usr/bin/env python3
"""Render named Mermaid blocks from maintained architecture documents."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

DIAGRAM_PATTERN = re.compile(
    r"<!--\s*diagram:\s*([a-z0-9-]+)\s*-->\s*"
    + r"\x60\x60\x60mermaid\s*\n(.*?)\x60\x60\x60",
    re.DOTALL,
)

DEFAULT_SOURCES = [Path("docs/architecture.md"), Path("docs/threat-model.md")]


def discover_diagrams(sources: list[Path]) -> list[tuple[str, str]]:
    diagrams: list[tuple[str, str]] = []
    for source in sources:
        content = source.read_text(encoding="utf-8")
        diagrams.extend(DIAGRAM_PATTERN.findall(content))

    if not diagrams:
        raise SystemExit("No named Mermaid diagrams found")

    names = [name for name, _ in diagrams]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise SystemExit("Diagram names must be unique: {}".format(", ".join(duplicates)))
    return diagrams


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="*", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--output-dir", type=Path, default=Path("build/architecture"))
    parser.add_argument(
        "--mmdc",
        default="npx --no-install mmdc",
        help="Command used to invoke the locally installed, lockfile-pinned Mermaid CLI.",
    )
    parser.add_argument(
        "--puppeteer-no-sandbox",
        action="store_true",
        help="Disable the Chromium sandbox for restricted CI runners only.",
    )
    args = parser.parse_args()

    diagrams = discover_diagrams(args.sources)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    command = shlex.split(args.mmdc)

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
