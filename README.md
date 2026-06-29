# Aktenfux

> A local, privacy-first document assistant for OCR-ready PDFs.

Aktenfux (`afu`) reads OCR text from PDF files, analyzes documents with a **local LLM via Ollama**, creates summaries, extracts metadata, and suggests safe filenames and archive locations. Documents remain on your machine at all times and are only permanently archived after **explicit user review**.

---

## 1. What is Aktenfux?

- A local tool for analyzing and sorting PDF documents.
- Uses **existing OCR text** already embedded in the PDF (no cloud OCR).
- Uses a **local LLM via Ollama** – no API keys, no external services.
- Documents **never leave your machine**.
- Conservative by default: files are staged in `_Review` first, then approved by you.
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

# Edit config.yaml if needed (e.g. adjust base_dir)
# By default, dry_run is ON – safe to explore.

# Verify your setup (Ollama, model, folders)
afu setup

# Test run – shows what would happen, moves nothing
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
| `ollama_url` | `http://localhost:11434` | Ollama API endpoint |
| `ollama_model` | `qwen3:8b` | Model to use for analysis |
| `dry_run` | `true` | **Safety default** – no files are moved |
| `split_dir` | `_Split` | Folder for approved documents recommended for split detection |
| `max_chars_for_llm` | `12000` | OCR text truncation limit |
| `language` | `de` | Summary language (`de` or `en`) |
| `write_markdown_summary` | `false` | Write `.md` summary next to sidecar JSON |
| `use_sqlite_index` | `false` | Enable optional SQLite index |

---

## 5. CLI Reference

| Command | Description |
|---------|-------------|
| `afu init` | Create config.yaml and working folders |
| `afu setup` | Check Ollama, model, and folder setup |
| `afu scan` | Process PDFs from `_Inbox` |
| `afu scan --dry-run` | Preview without moving files |
| `afu review` | List documents awaiting approval |
| `afu approve <id>` | Archive an approved document, or stage it in `_Split` when split detection is recommended |
| `afu approve --all` | Archive/stage all documents currently in `_Review` |
| `afu reject <id>` | Move a document to `_Error` |
| `afu reject --all` | Move all documents currently in `_Review` to `_Error` |
| `afu status` | Show document counts per folder |
| `afu reprocess <id>` | Re-analyze a document with the LLM |

---

## 6. Security

- Aktenfux works **entirely offline**.
- Documents are **never sent to external APIs**.
- Ollama runs **locally** on your machine.
- The default mode (`dry_run: true`) is conservative – nothing moves until you decide.
- Documents are only permanently archived **after you approve them**.
- Sidecar JSON stays next to each PDF as a transparent audit trail.
- SHA-256 hashing detects duplicates before re-importing.
- All file moves are validated to stay within `base_dir` (no path traversal).
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
