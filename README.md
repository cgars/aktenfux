# Aktenfux

> A local, privacy-first document assistant for OCR-ready PDFs.

Aktenfux (`afu`) reads OCR text from PDF files, analyzes documents via an Ollama endpoint, creates summaries, extracts metadata, and suggests filenames and archive locations. The default endpoint is local, but the current MVP does not enforce that boundary: configuring a LAN or internet URL transmits OCR text and summaries to that endpoint. The target lifecycle permanently archives documents only after **explicit user review**. The current MVP neither enforces distinct, non-overlapping lifecycle roots nor prevents model-supplied filenames and folders from escaping `_Review` within `base_dir`; either gap can bypass that boundary.

---

## 1. What is Aktenfux?

- A local tool for analyzing and sorting PDF documents.
- Uses **existing OCR text** already embedded in the PDF (no cloud OCR).
- Defaults to a **local LLM via Ollama** – no API key is required.
- Documents remain on your machine only while `ollama_url` is a loopback endpoint. The current MVP accepts non-loopback URLs, which send document-derived content to another host.
- Target behavior is conservative: files are staged in `_Review` first, then approved by you. Until lifecycle-root validation and locally derived destination paths are implemented, unsafe configuration or a model-derived path can bypass this guarantee.
- Works with ScanSnap scans or any OCR-processed PDF.
- Stores scan results in a human-readable **sidecar JSON** next to each PDF.
- Optional **SQLite index** for status tracking and duplicate detection.

---

## 2. Initial Development Quick Start

### Prerequisites

1. **Python 3.12** or later
2. **Ollama** – install from <https://ollama.com>
3. Pull a model manually (models can be several GB):
   ```bash
   ollama pull qwen3:8b
   ```

### Installation

```bash
# Clone the repository
git clone https://github.com/cgars/aktenfux.git
cd aktenfux

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate      # Linux / macOS
# .venv\Scripts\activate       # Windows

# Install dependencies
pip install -r requirements.txt

# Install the package in editable mode (activates the `afu` command)
pip install -e .
```

### First Run

```bash
# Create config.yaml and working folders
afu init

# Edit config.yaml if needed (e.g. adjust base_dir).
# Keep ollama_url on loopback; other endpoints receive OCR text and summaries.
# dry_run is ON by default, but current scan dry-run still writes model-named JSON
# and is not safe for real documents until the documented release gate is fixed.

# Verify your setup (Ollama, model, folders)
afu setup

# Test with synthetic, disposable input only. The PDF is not moved, but the
# current MVP writes a JSON result whose model-supplied name can escape _DryRun.
afu scan --dry-run

# Real import – disable dry_run in config.yaml (or pass --no-dry-run) to actually move files
afu scan --no-dry-run

# Review imported documents
afu review

# Approve a document (moves it to Archive)
afu approve <id>

# Reject a document (moves it to _Error)
afu reject <id>

# Show folder counts
afu status
```

All commands are also available as:

```bash
python -m aktenfux scan
python -m aktenfux review
python -m aktenfux approve <id>
```

---

## 3. Folder Structure

```
<base_dir>/               (default: ~/Documents/Aktenfux)
├── _Inbox/               Drop new PDFs here
├── _Review/              Waiting for your approval
├── _Imported/            (reserved)
├── _Error/               Files that could not be processed
├── _Split/               Approved scans recommended for splitting
└── Archive/
    └── <category>/
        └── <correspondent>/
            └── <topic>/
                ├── 2026-06-20_HUK-COBURG_Invoice_Car-Insurance.pdf
                └── 2026-06-20_HUK-COBURG_Invoice_Car-Insurance.json
```

---

## 4. Configuration

Copy `config.example.yaml` to `config.yaml` (or run `afu init`) and adjust as needed:

| Key | Default | Description |
|-----|---------|-------------|
| `base_dir` | `~/Documents/Aktenfux` | Root folder for all working directories |
| `ollama_url` | `http://localhost:11434` | Ollama endpoint. Keep it on loopback; a LAN/internet URL receives OCR text and summaries, potentially over plaintext HTTP. |
| `ollama_model` | `qwen3:8b` | Model to use for analysis |
| `dry_run` | `true` | PDFs are not moved, but current scan dry-run initializes/accesses SQLite when enabled and persists model-named JSON; it is not mutation-free. |
| `split_dir` | `_Split` | Folder for approved documents recommended for split detection |
| `max_chars_for_llm` | `12000` | OCR text truncation limit |
| `language` | `de` | Summary language (`de` or `en`) |
| `write_markdown_summary` | `false` | Write `.md` summary next to sidecar JSON |
| `use_sqlite_index` | `false` | Enable optional SQLite index |

Current MVP warning: keep every lifecycle directory setting simple, relative, unique, and non-overlapping. In particular, `_Review` must not equal, contain, or sit inside `Archive`; this validation is a release gate rather than an implemented guarantee.

---

## 5. CLI Reference

| Command | Description |
|---------|-------------|
| `afu init` | Create config.yaml and working folders |
| `afu setup` | Check Ollama, model, and folder setup |
| `afu scan` | Process PDFs from `_Inbox` |
| `afu scan --dry-run` | Do not move the PDF; current MVP still initializes/accesses SQLite when enabled and writes unsafe model-named JSON (synthetic data only). |
| `afu review` | List documents awaiting approval |
| `afu approve <id>` | Archive an approved document, or stage it in `_Split` when split detection is recommended |
| `afu approve --all` | Archive/stage all documents currently in `_Review` |
| `afu reject <id>` | Move a document to `_Error` |
| `afu reject --all` | Move all documents currently in `_Review` to `_Error` |
| `afu status` | Show document counts per folder |
| `afu reprocess <id>` | Re-analyze a document with the LLM |

---

## 6. Security

- Aktenfux is **local-first**, not unconditionally offline. The default Ollama endpoint is loopback.
- The current MVP does not enforce loopback-only inference. A configured LAN or internet endpoint receives OCR text and summaries and may use plaintext HTTP; remote inference is unsupported for the first release.
- Current scan dry-run does not move the source PDF, but it initializes/accesses SQLite when enabled, creates `_DryRun`, and writes a model-named JSON result that can escape that directory and replace another file. Use only synthetic, disposable input until mutation-free dry-run is implemented.
- Current normal scans can silently overwrite existing same-stem inbox `.json` and optional `.md` siblings before move collision handling. Keep the inbox free of sibling artifacts and retain backups until fixed.
- See the [architecture](docs/architecture.md) and [threat model](docs/threat-model.md) for current gaps and release gates.
- Target behavior permanently archives documents **only after you approve them**; current lifecycle-root aliasing and model-derived path escape gaps can bypass this guarantee.
- Sidecar JSON stays next to each PDF as a transparent audit trail.
- SHA-256 hashing detects duplicates before re-importing.
- Normal moves have broad `base_dir` checks, but current pre-read, sibling-write, exact-root, and model-derived-path gaps remain release blockers.
- Lifecycle roots are not yet checked for equality, nesting, case/Unicode aliases, or shared filesystem identity; an aliased `_Review`/`Archive` can bypass approval.
- A confined PDF does not make same-stem JSON/Markdown companions safe: companion symlinks and wrong extensions can escape or alias/replace the PDF.
- **Backup your document folder** – Aktenfux is a tool, not a backup solution.

---

## 7. Architecture

- **Sidecar JSON** is the source of truth per document (stored alongside the PDF).
- **SQLite** is an optional index only – the tool works without it.
- **Core logic is cross-platform** (Windows, macOS, Linux).
- All paths use `pathlib` – no hard-coded OS-specific separators.
- OS-specific setup scripts and installers are **intentionally out of scope** for v0.1.

### Module Overview

| Module | Purpose |
|--------|---------|
| `config.py` | Load and validate `config.yaml` |
| `schema.py` | Pydantic models for LLM output and sidecar JSON |
| `pdf_text.py` | Extract OCR text from PDFs |
| `llm.py` | Build prompts, call Ollama, validate response |
| `ollama_manager.py` | Check Ollama availability, list/pull/test models |
| `filenames.py` | Safe filename and archive path generation |
| `storage.py` | SHA-256, sidecar I/O, file moves with safety checks |
| `db.py` | Optional SQLite index |
| `review.py` | List and display `_Review` documents |
| `main.py` | Core processing pipeline |
| `cli.py` | Typer-based CLI (`afu`) |

---

## 8. Development

```bash
# Install with dev extras
pip install -e ".[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=aktenfux --cov-report=term-missing
```

---

## 9. Supported Models

| Model | Size | Notes |
|-------|------|-------|
| `qwen3:8b` | ~5 GB | Default, good JSON output |
| `llama3.1:8b` | ~5 GB | Alternative |

The model is configurable via `ollama_model` in `config.yaml`.
