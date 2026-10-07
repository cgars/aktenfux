#!/usr/bin/env python3
"""Run the repository's repeatable pre-push verification harness."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def run(label: str, command: list[str]) -> None:
    print(f"\n==> {label}", flush=True)
    subprocess.run(command, cwd=REPO_ROOT, check=True)


def ref_exists(ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="Git ref used for committed branch-diff checks (default: origin/main).",
    )
    args = parser.parse_args()

    run("Python tests", [sys.executable, "-m", "pytest", "-q"])
    run(
        "Python compilation",
        [sys.executable, "-m", "compileall", "-q", "aktenfux", "tests", "scripts"],
    )
    run("Repository checks", [sys.executable, "scripts/check_repository.py"])
    run("Unstaged diff whitespace", ["git", "diff", "--check"])
    run("Staged diff whitespace", ["git", "diff", "--cached", "--check"])
    if ref_exists(args.base_ref):
        run(
            f"Committed diff whitespace against {args.base_ref}",
            ["git", "diff", "--check", f"{args.base_ref}...HEAD"],
        )
    else:
        print(
            f"\nERROR: base ref {args.base_ref!r} does not exist; fetch it or pass --base-ref.",
            file=sys.stderr,
        )
        return 1

    print("\nVerification passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc
