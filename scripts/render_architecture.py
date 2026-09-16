#!/usr/bin/env python3
"""Render named Mermaid blocks from ARCHITECTURE.md to SVG files."""

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("ARCHITECTURE.md"))
    parser.add_argument(
        "--output-dir", type=Path, default=Path("build/architecture")
    )
    parser.add_argument(
        "--mmdc",
        default="npx --yes @mermaid-js/mermaid-cli@11.12.0",
        help="Command used to invoke Mermaid CLI.",
    )
    parser.add_argument(
        "--puppeteer-no-sandbox",
        action="store_true",
        help="Disable the Chromium sandbox for restricted CI runners only.",
    )
    args = parser.parse_args()

    source = args.source.read_text(encoding="utf-8")
    diagrams = DIAGRAM_PATTERN.findall(source)
    if not diagrams:
        raise SystemExit("No named Mermaid diagrams found in {}".format(args.source))

    names = [name for name, _ in diagrams]
    if len(names) != len(set(names)):
        raise SystemExit("Diagram names must be unique: {}".format(", ".join(names)))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    command = shlex.split(args.mmdc)

    with tempfile.TemporaryDirectory(prefix="architecture-") as temp_dir:
        temp_path = Path(temp_dir)
        browser_args = []
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
            input_path = temp_path / "{}.mmd".format(name)
            output_path = args.output_dir / "{}.svg".format(name)
            input_path.write_text(diagram.strip() + "\n", encoding="utf-8")
            subprocess.run(
                command
                + browser_args
                + [
                    "-i",
                    str(input_path),
                    "-o",
                    str(output_path),
                    "-b",
                    "transparent",
                ],
                check=True,
            )
            print("generated {}".format(output_path))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
