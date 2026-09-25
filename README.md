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
- Normally writes scan results to a human-readable **sidecar JSON** beside the PDF; current interrupted or partial operations can separate the pair.
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
# dry_run is ON by default, but current scan and reprocess dry-run still write
# model-named JSON and may access/create SQLite state. They are not safe for real
# documents until the documented release gate is fixed.

# Verify your setup (Ollama, model, folders)
afu setup

# Test with synthetic, disposable input only. Before checking the inbox, scan
# contacts Ollama and, when reachable, lists models and can prompt for a
# persistent model download.
# The PDF is not moved, but after successful analysis the current MVP writes a
# JSON result whose model-supplied name can escape _DryRun.
afu scan --dry-run

# Non-dry-run import moves files. This pre-release path still has documented
# release blockers; use synthetic, disposable input only.
afu scan --no-dry-run

# Review documents that reached _Review
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
| `dry_run` | `true` | PDFs are not moved, but current scan and reprocess dry-run can create/access SQLite state and persist model-named JSON; they are not mutation-free. |
| `split_dir` | `_Split` | Folder for approved documents recommended for split detection |
| `max_chars_for_llm` | `12000` | OCR text truncation limit |
| `language` | `de` | Summary language (`de` or `en`) |
| `write_markdown_summary` | `false` | Write a sensitive `.md` summary next to sidecar JSON. Current reject/error paths can leave it orphaned in the prior lifecycle area. |
| `use_sqlite_index` | `false` | Enable optional SQLite index |

Current MVP warning: keep every lifecycle directory setting simple, relative, unique, and non-overlapping. In particular, `_Review` must not equal, contain, or sit inside `Archive`; this validation is a release gate rather than an implemented guarantee. Keep YAML booleans unquoted: the current loose coercion treats a non-empty string such as `"false"` as true and can enable a feature unexpectedly.

---

## 5. CLI Reference

| Command | Description |
|---------|-------------|
| `afu init` | Create config.yaml and working folders |
| `afu setup` | Check Ollama, model, and folder setup |
| `afu scan` | Process PDFs from `_Inbox` |
| `afu scan --dry-run` | Before inbox inspection, contact Ollama and, when reachable, list models, optionally prompting for a confirmed persistent model download. With any PDF and indexing enabled, the current MVP initializes/accesses SQLite; after successful analysis it writes unsafe model-named JSON (synthetic data only). |
| `afu review` | List documents awaiting approval |
| `afu approve <id>` | Archive an approved document, or stage it in `_Split` when split detection is recommended |
| `afu approve --all` | Archive/stage all documents currently in `_Review` |
| `afu reject <id>` | Move a document to `_Error` |
| `afu reject --all` | Move all documents currently in `_Review` to `_Error` |
| `afu status` | Show document counts per folder |
| `afu reprocess <id>` | Re-analyze a Review document with the LLM. The current command returns no structured success/error receipt; inspect `_Review`, `_Error`, and logs. With dry-run enabled and usable OCR it can create/access SQLite state, and after successful analysis it writes unsafe model-named JSON. |

---

## 6. Security

- Aktenfux is **local-first**, not unconditionally offline. The default Ollama endpoint is loopback.
- The current MVP does not enforce loopback-only inference. A configured LAN or internet endpoint receives OCR text and summaries and may use plaintext HTTP; remote inference is unsupported for the first release.
- Every scan contacts the configured Ollama endpoint before checking whether the inbox contains a PDF. When reachable, it lists models; if the configured model is absent, an accepted prompt downloads and persists it even for an empty inbox or scan dry-run.
- Current scan and reprocess dry-run do not move the source PDF, but they can create/access SQLite state, create `_DryRun`, and write a model-named JSON result that can escape that directory and replace another file. Use only synthetic, disposable input until mutation-free dry-run is implemented.
- Current normal scans can silently overwrite existing same-stem inbox `.json` and optional `.md` siblings before move collision handling. Keep the inbox free of sibling artifacts and retain backups until fixed.
- See the [architecture](docs/architecture.md) and [threat model](docs/threat-model.md) for current gaps and release gates.
- Target behavior permanently archives documents **only after you approve them**; current lifecycle-root aliasing and model-derived path escape gaps can bypass this guarantee.
- Target behavior keeps each PDF and sidecar together as a recoverable audit unit; current sequential writes and moves can leave partial or separated state.
- When the optional SQLite index is enabled, SHA-256 can flag a potential duplicate, but the current scan still proceeds and the check is not an import-prevention guarantee.
- Normal moves have broad `base_dir` checks, but current pre-read, sibling-write, exact-root, and model-derived-path gaps remain release blockers.
- Lifecycle roots are not yet checked for equality, nesting, case/Unicode aliases, or shared filesystem identity; an aliased `_Review`/`Archive` can bypass approval.
- A confined PDF does not make same-stem JSON/Markdown companions safe: companion symlinks and wrong extensions can escape or alias/replace the PDF. With Markdown enabled, reject and scan-error paths currently leave the `.md` behind when PDF/JSON move to `_Error`.
- **Backup your document folder** – Aktenfux is a tool, not a backup solution.

---

## 7. Architecture

- The target data model treats **sidecar JSON** as the source of truth per document and keeps it beside the PDF; current partial operations can separate the pair.
- **SQLite** is an optional derived index – the tool works without it, but automatic rebuild is not implemented yet.
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
| `filenames.py` | Filename/path helper generation and sanitization; not yet the sole destination authority |
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
