"""Configuration loading and validation for Aktenfux."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_CONFIG: dict[str, Any] = {
    "base_dir": "~/Documents/Aktenfux",
    "inbox_dir": "_Inbox",
    "review_dir": "_Review",
    "imported_dir": "_Imported",
    "error_dir": "_Error",
    "archive_dir": "Archive",
    "dry_run_dir": "_DryRun",
    "ollama_url": "http://localhost:11434",
    "ollama_model": "qwen3:8b",
    "ollama_timeout": 120.0,
    "dry_run": True,
    "max_chars_for_llm": 12000,
    "language": "de",
    "write_markdown_summary": False,
    "use_sqlite_index": False,
    "sqlite_path": "aktenfux.db",
    "allowed_top_level_categories": [
        "Taxes",
        "Banking",
        "Insurance",
        "Home",
        "Car",
        "Work",
        "Health",
        "Contracts",
        "Invoices",
        "Warranties",
        "Other",
    ],
}

_CONFIG_FILENAME = "config.yaml"
_EXAMPLE_FILENAME = "config.example.yaml"

# Resolved at import time – the directory where pyproject.toml lives.
_PACKAGE_ROOT = Path(__file__).parent.parent.resolve()


class AktenfuxConfig:
    """Holds all runtime configuration values."""

    def __init__(self, data: dict[str, Any]) -> None:
        merged = {**_DEFAULT_CONFIG, **data}

        self.base_dir: Path = Path(merged["base_dir"]).expanduser().resolve()
        self.inbox_dir: str = merged["inbox_dir"]
        self.review_dir: str = merged["review_dir"]
        self.imported_dir: str = merged["imported_dir"]
        self.error_dir: str = merged["error_dir"]
        self.archive_dir: str = merged["archive_dir"]
        self.dry_run_dir: str = merged["dry_run_dir"]

        self.ollama_url: str = merged["ollama_url"]
        self.ollama_model: str = merged["ollama_model"]
        self.ollama_timeout: float = float(merged["ollama_timeout"])

        self.dry_run: bool = bool(merged["dry_run"])
        self.max_chars_for_llm: int = int(merged["max_chars_for_llm"])
        self.language: str = merged["language"]
        self.write_markdown_summary: bool = bool(merged["write_markdown_summary"])
        self.use_sqlite_index: bool = bool(merged["use_sqlite_index"])

        sqlite_raw = merged["sqlite_path"]
        sqlite_path = Path(sqlite_raw)
        if not sqlite_path.is_absolute():
            sqlite_path = self.base_dir / sqlite_path
        self.sqlite_path: Path = sqlite_path.resolve()

        self.allowed_top_level_categories: list[str] = list(
            merged["allowed_top_level_categories"]
        )

    # ------------------------------------------------------------------
    # Derived path helpers
    # ------------------------------------------------------------------

    @property
    def inbox_path(self) -> Path:
        return self.base_dir / self.inbox_dir

    @property
    def review_path(self) -> Path:
        return self.base_dir / self.review_dir

    @property
    def imported_path(self) -> Path:
        return self.base_dir / self.imported_dir

    @property
    def error_path(self) -> Path:
        return self.base_dir / self.error_dir

    @property
    def archive_path(self) -> Path:
        return self.base_dir / self.archive_dir

    @property
    def dry_run_path(self) -> Path:
        return self.base_dir / self.dry_run_dir

    def all_working_dirs(self) -> list[Path]:
        return [
            self.inbox_path,
            self.review_path,
            self.imported_path,
            self.error_path,
            self.archive_path,
            self.dry_run_path,
        ]


def _find_config_file() -> Path | None:
    """Search for config.yaml starting from cwd upward, then package root."""
    candidates = [
        Path.cwd() / _CONFIG_FILENAME,
        _PACKAGE_ROOT / _CONFIG_FILENAME,
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def load_config(config_path: Path | None = None) -> AktenfuxConfig:
    """Load configuration from *config_path* or auto-discover config.yaml.

    Raises FileNotFoundError when no config.yaml is found and
    *config_path* was not explicitly provided.
    """
    if config_path is None:
        config_path = _find_config_file()
        if config_path is None:
            raise FileNotFoundError(
                "No config.yaml found. Run 'afu init' to create one."
            )

    with config_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    return AktenfuxConfig(data)


def init_config(target_dir: Path | None = None) -> Path:
    """Copy config.example.yaml → config.yaml if it does not exist yet.

    Returns the path of the config file (whether newly created or pre-existing).
    """
    if target_dir is None:
        target_dir = _PACKAGE_ROOT

    example = _PACKAGE_ROOT / _EXAMPLE_FILENAME
    dest = target_dir / _CONFIG_FILENAME

    if dest.exists():
        return dest

    if not example.exists():
        raise FileNotFoundError(
            f"config.example.yaml not found at {example}. "
            "Cannot initialise configuration."
        )

    shutil.copy2(example, dest)
    return dest
