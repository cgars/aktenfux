# Search archived PDFs and Aktenfux sidecars

`scripts/open_json_pdfs.py` is a **read-only**, standalone search helper. It searches
PDFs recursively, matching literal search terms case-insensitively. By default,
every term must occur somewhere in the JSON sidecar. With `--pdf-text`, any
missing terms can alternatively be found in the PDF's embedded/OCR text.

Aktenfux convention: `document.pdf` + `document.json` (the helper also
recognizes `document.pdf.json`). It does not require installing Aktenfux, an
Ollama service, or a database.

```bash
python3 scripts/open_json_pdfs.py ~/Documents/Aktenfux "Telekom" "Rechnung"
python3 scripts/open_json_pdfs.py ~/Documents/Aktenfux --pdf-text "Vertragsnummer"
python3 scripts/open_json_pdfs.py ~/Documents/Aktenfux --list "Freiburg"
python3 scripts/open_json_pdfs.py ~/Documents/Aktenfux --paths "Freiburg"
```

For PDF text search, install `pdftotext` (Debian/Ubuntu: `sudo apt
install poppler-utils`). PDFs containing only images and no embedded OCR
layer cannot be searched by this helper.

With multiple matches in an interactive terminal, the optional `fzf` picker
provides a filterable list. Enter opens the selected PDF using the OS default
viewer. With one match, the file opens directly. Without `fzf`, you can enter
a result number. `--list` and `--paths` disable interactive opening.

On WSL, the helper uses `wslpath` and `explorer.exe` to hand off PDFs to
Windows. On macOS/Linux, it uses `open` / `xdg-open`.

## Safety and limits

- The helper does not modify PDFs, sidecars, indexes, or the filesystem
  beyond normal subprocess/viewer effects.
- Directory symlinks are not followed; symlinked PDFs and sidecars are
  skipped to avoid unexpectedly reading outside the chosen tree.
- All matching happens locally. Document titles, metadata, and paths of
  matches appear in the invoking terminal. `fzf` also receives matching
  filenames and summaries locally.
- Matches are based on JSON text (including machine-generated metadata),
  not the truth or correctness of the LLM's conclusions.
- `--pdf-text` executes the local `pdftotext` parser against untrusted
  PDFs. Update Poppler and avoid processing hostile files with high privileges.

This is a utility script, not an `afu` CLI command or indexed search service.
