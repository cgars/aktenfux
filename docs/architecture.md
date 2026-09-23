# Aktenfux architecture

Status: maintained current-state and target-state baseline  
Last reviewed: 2026-09-21  
Owner: project maintainers

Aktenfux is a local-first document processing and review workbench for OCR-ready
PDFs. It extracts embedded text, asks a configured inference provider for
structured proposals, validates those proposals, and stages documents for human
review before archival.

This document distinguishes the implementation currently present on `main` from
planned architecture. A planned box in a diagram is not a claim that the feature
already exists.

## ELI5

Aktenfux is like a careful clerk. You place a letter in an inbox. The clerk reads
it, writes a suggested label on a separate index card, and puts both into a
review tray. The clerk may suggest where the letter belongs, but only you may
approve moving it into the archive.

The letter is the original PDF. The index card is the sidecar JSON. The optional
SQLite database is only a catalogue: losing the catalogue must not mean losing
the letter or its authoritative index card.

## Architectural principles

1. **Local first.** Private document content stays on systems explicitly chosen
   by the operator. Local inference is the default security boundary.
2. **Human-controlled archival.** Analysis creates proposals; explicit approval
   performs permanent archival.
3. **Sidecar authority.** The JSON sidecar is the authoritative Aktenfux record
   for a document. SQLite is a derived index only.
4. **Untrusted inputs.** PDFs, OCR text, model output, sidecars, configuration,
   filenames, and synchronized-folder events are validated at their boundaries.
5. **Deterministic filesystem control.** Semantic model output may inform local
   naming functions, but it must not directly control filesystem paths.
6. **Recoverable operations.** File and sidecar changes must be observable,
   retryable, and recoverable after interruption.
7. **One application core.** CLI, review UI, and future MCP tools call the same
   application services rather than reimplementing file operations.
8. **Explainable limits.** Confidence is not certainty, a summary is not the
   source document, and a model suggestion is not a verified fact.

## Current system context

<!-- diagram: current-system-context -->
```mermaid
flowchart TD
    U["Local user"] --> CLI["afu CLI"]
    CLI --> CORE["Aktenfux processing core"]
    CORE --> PDF["PDF parser libraries"]
    CORE --> OLLAMA["Configured Ollama endpoint"]
    CORE --> STORE["Local document workspace"]
    CORE --> DB[("Optional SQLite index")]
```

The CLI is the interface currently present on `main`. A browser workbench exists
separately as draft work and is not part of the current architecture. The
configured Ollama URL defaults to loopback, but the implementation currently
allows other URLs without an explicit remote-inference opt-in.

## Current processing flow

<!-- diagram: current-document-flow -->
```mermaid
flowchart TD
    IN["PDF discovered in Inbox"] --> TEXT["Extract embedded OCR text"]
    TEXT --> HASH["Calculate SHA-256"]
    HASH --> LLM["Two-pass model analysis"]
    LLM --> MODEL["Pydantic validation and coercion"]
    MODEL --> SIDE["Write JSON sidecar"]
    SIDE --> MOVE["Validate paths during move"]
    MOVE --> REVIEW["PDF and sidecar in Review"]
    REVIEW -->|"Approve"| FINAL["Archive or Split staging"]
    REVIEW -->|"Reject"| ERROR["Error folder"]
```

This diagram deliberately reflects the current order. Source-path validation is
not performed before PDF extraction and hashing. A symlink or other unexpected
filesystem object in `_Inbox` may therefore be read before the later move check
rejects it. This is a documented security gap, not the intended target behavior.

Dry-run follows the analysis path without moving the original PDF. When SQLite
is enabled it currently initializes or updates the schema and performs duplicate
index access; it also creates `_DryRun` and writes a sidecar there. Those state
changes violate the target dry-run invariant. A likely multi-document scan is
staged in `_Split` only after approval; the current pipeline does not silently
split it.

## Intended secure processing flow

<!-- diagram: target-document-flow -->
```mermaid
flowchart TD
    IN["Candidate in Inbox"] --> GUARD["Validate type, root, symlink, size and limits"]
    GUARD --> SNAP["Open stable input and calculate identity hash"]
    SNAP --> TEXT["Extract page-aware OCR evidence"]
    TEXT --> INF["Local-first inference gateway"]
    INF --> VALID["Validate structured proposal"]
    VALID --> DERIVE["Derive safe names and exact target roots locally"]
    DERIVE --> STAGE["Atomically stage PDF and sidecar for review"]
    STAGE --> HUMAN["Human review and explicit command"]
    HUMAN -->|"Approve"| FINAL["Recoverable archive or Split staging"]
    HUMAN -->|"Reject"| ERROR["Recoverable Error staging"]
```

The target flow validates the source before reading it, separates model-proposed
semantics from trusted path derivation, preserves page-level evidence, and treats
the PDF plus sidecar as one recoverable document unit. In dry-run mode it stops
before staging and returns the proposal and planned changes without persisting
artifacts, index rows, or lifecycle state.

## Current components

| Component | Current responsibility | Boundary notes |
|---|---|---|
| `config.py` | Load configuration and construct workspace paths | Directory names, `sqlite_path`, and inference URL require stronger validation; the index path is not currently confined to an exact approved root. |
| `pdf_text.py` | Extract embedded text with pypdf and optional pdfplumber | Processes hostile PDFs without explicit size/page/time limits. |
| `pdf_metadata.py` | Configurable PDF metadata rewriting | Disabled by default but currently opt-in through configuration; unsupported for the first release because it changes file bytes without refreshing the stored hash. |
| `llm.py` | Two-pass summarization and structured extraction | OCR and model output are untrusted; configured HTTP endpoint may be remote. |
| `ollama_manager.py` | Check, list, pull, and test models | Model downloads require confirmation; network boundary must remain explicit. |
| `schema.py` | Validate analysis and sidecar models | Validation normalizes shape, not factual correctness. |
| `filenames.py` | Generate and sanitize names | Must become the sole authority for trusted destination names. |
| `storage.py` | Hashing, sidecar I/O, path checks, and moves | Writes and paired moves are not currently transactional. |
| `db.py` | Optional status and duplicate index | Derived in principle; automatic rebuild and pre-access exact-root validation for the configured index path are not implemented on `main`. |
| `main.py` | Processing, approve, reject, and reprocess orchestration | Mixes application policy and I/O sequencing. |
| `review.py` | Resolve and display review records | Sidecar data remains untrusted even when locally stored. |
| `cli.py` | User commands and terminal presentation | Bulk actions need explicit consequence handling. |

## Current implementation gap inventory

This table groups known gaps by execution surface so fixes and tests cover whole
classes rather than isolated examples.

| Surface | Current behavior | Required release-gate behavior |
|---|---|---|
| Configuration | Lifecycle directory names, `sqlite_path`, and `ollama_url` are not fully constrained. Roots may be equal, nested, absolute, or filesystem aliases; `_Review` can therefore alias `Archive` and bypass approval. | Validate typed relative configuration before I/O; require lifecycle/index roots to be pairwise distinct, non-overlapping, non-symlink children of `base_dir`; enforce loopback inference for the first release. |
| Candidate intake | PDF parsing, hashing, and inference occur before exact-inbox confinement. | Validate regular file, link status, exact root, size, and stable identity before any content access or transmission. |
| Scan dry-run | Can initialize/access SQLite, create `_DryRun`, and write or overwrite an untrusted model-named JSON file. | Return a proposal without directory, artifact, database, index, or lifecycle access/mutation. |
| Normal scan | Pre-move JSON and optional Markdown writes can overwrite same-stem inbox siblings; wrong model extensions can make PDF and companion destinations identical. | Canonical extensions; independently validated, pairwise-distinct artifact paths; collision-safe atomic no-overwrite staging of the whole document unit. |
| Review and lifecycle commands | Persisted sidecar validation errors can expose values; companion symlinks can escape independently of a confined PDF; model-derived paths and sequential moves remain trusted too broadly. | Redacted diagnostics; strict sidecar schema/lifecycle validation; locally derived destinations; independent exact-root/link/type/identity checks for every artifact; recoverable transitions. |
| Logging | INFO can expose complete paths; WARNING/ERROR exceptions can expose values; DEBUG can expose summaries, raw responses, and PDF metadata. | Content-free structured diagnostic codes at every level; sensitive diagnostics require an explicit, bounded export workflow. |
| SQLite | Even nominal reads use a connection helper that can create parent directories or the database; the configured path is not confined and can alias a lifecycle root or document artifact. | Dedicated non-overlapping index location and identity validation before access; read-only operations cannot create state; tested rebuild and reconciliation. |
| Schema boundaries | Most model/sidecar strings and collections are unbounded; malformed amounts become zero and other invalid values are silently coerced or dropped. | Strict size/shape limits and explicit invalid/proposed/confirmed states for consequential values. |
| Terminal output | Untrusted model, sidecar, filename, and endpoint text reaches Rich, logs, and raw output without consistent control-character escaping or bounds. | Escape controls, disable markup for evidence, bound output, and keep trusted prompts visually separate. |
| PDF metadata | Opt-in rewriting changes bytes after the recorded hash, logs metadata values at DEBUG, and uses a predictable non-exclusive sibling temporary path. | Unsupported and disabled until a later ADR defines coherent integrity, secure temporary-file handling, provenance, logging, and recovery semantics. |
| Timestamps | Lifecycle timestamps are local and timezone-naive. | Canonical timezone-aware UTC plus explicit legacy migration semantics. |
| Toolchain | CI actions and npm graph are pinned; renderer fallback must still fail closed when local Mermaid is absent. | Invoke only the lockfile-installed binary and stop before any registry lookup when it is missing. |
| Python and model supply chain | Runtime/build dependencies have open lower bounds without a lock; model artifacts lack recorded integrity provenance. | Reproducible locked Python graph, vulnerability review, and verified/versioned model provenance. |

## Data ownership and recoverability

| Data | Authority | Rebuildable today? | Sensitivity and notes |
|---|---|---:|---|
| Original PDF | User-controlled filesystem | No | Primary private record; immutable by default. |
| Sidecar JSON | Aktenfux source of truth | No | Contains summaries, entities, paths, amounts, deadlines, model proposals, and warnings; current repair warnings may embed document-derived values from validation exceptions and require redaction. Current `processed_at` and `approved_at` values are timezone-naive local timestamps, so their meaning can be ambiguous across hosts and daylight-saving transitions. |
| Markdown summary | Derived presentation | Yes, from sidecar | Optional and sensitive. |
| SQLite index | Derived index | Not automatically | Must contain no unique state; rebuild capability remains planned. |
| OCR text in memory | Derived transient data | Yes | Private; not stored in the sidecar by default. |
| Model response | Untrusted input | Yes | Debug logging can persist the complete response; even without debug, Pydantic exceptions can expose document-derived values in normal logs and can be copied into persistent sidecar repair warnings. Target diagnostics must use redacted codes and bounded context. |
| PDF metadata | Optional embedded derivative | Only with care | Rewrites PDF bytes and may travel when the PDF is shared. |
| Archive layout | Human-approved organization | From PDF and sidecar | Wrong moves can still impose significant recovery cost. |

"Rebuildable in principle" is not the same as "rebuildable today." Until an
index rebuild command exists and is tested, loss or drift of SQLite requires
manual recovery even though no unique business state should live there.

## Trust boundaries

| Boundary | Trust transition | Required rule |
|---|---|---|
| Inbox filesystem → Aktenfux | External or synchronized files enter the process | Validate the resolved source, type, size, page count, and stability before parsing. |
| PDF bytes → parser | Complex hostile format enters dependency code | Bound resources, fail closed, and preserve the input. |
| OCR text → model prompt | Document content enters an instruction-following system | Treat all OCR instructions as quoted document data, never authority. |
| Model response → domain model | Probabilistic output becomes structured data | Validate shape, retain uncertainty, and distinguish proposal from fact. |
| Semantic proposal → filesystem | Suggested meaning influences file placement | Derive names locally and validate against the exact destination root. |
| Config → network endpoint | Operator configuration chooses data destination | Local-only by default; remote transfer requires explicit informed opt-in. |
| Sidecar → review decision | Generated or edited data influences the user | Show origin, evidence, confidence, warnings, and conflicts. |
| CLI/UI/MCP → application command | An interface requests state change | Centralize policy; preview and confirm consequential writes. |
| Workspace → sync/backup/export | Sensitive artifacts leave process control | Document confidentiality, race, retention, and recovery assumptions. |

## Application service boundary — planned

<!-- diagram: target-components -->
```mermaid
flowchart TD
    CLI["CLI"] --> APP["Application services"]
    UI["Review workbench — planned"] --> APP
    MCP["MCP adapter — planned"] --> APP
    APP --> DOC["Document and evidence pipeline"]
    APP --> COMMANDS["Review and archive commands"]
    DOC --> INF["Inference provider interface"]
    INF --> OLLAMA["Ollama default"]
    INF --> LOCAL["Other explicitly configured local provider"]
    DOC --> STORE["PDFs, sidecars and derived index"]
    COMMANDS --> STORE
```

The application layer owns use cases such as scan, inspect, approve, reject,
reprocess, and later split. Interfaces present commands and results; they do not
move files, update sidecars, or decide authorization independently.

The future MCP adapter is a separate trust boundary. Its first release should
expose read-only inspection. Write tools require explicit, narrowly scoped
commands, exact document identifiers, previews, confirmation, audit events, and
no arbitrary filesystem or SQL access.

## Security and privacy baseline

The maintained [threat assessment](threat-model.md) defines assets, actors,
threats, required controls, verification, and release gates. Its controls are
requirements, not claims that they are already implemented.

The following architecture invariants apply:

- No input is trusted solely because it is inside `base_dir`.
- All source validation occurs before opening, parsing, hashing, or transmitting.
- All destination validation uses the exact allowed root for the operation.
- All lifecycle and index roots are pairwise distinct, non-overlapping,
  non-symlink children of `base_dir`, and are validated before any I/O.
- PDF, JSON, and Markdown paths have canonical extensions, are independently
  confined and link-checked, and are pairwise distinct by normalized path and
  filesystem identity before any operation begins.
- LLM output never directly authorizes a write or supplies a trusted path.
- No document enters `Archive` without an explicit human-approved command.
- Dry-run returns a proposal without persisting or mutating any document,
  sidecar, Markdown, index, or lifecycle state.
- PDF bytes remain unchanged by default. Metadata rewriting is unsupported for
  the first release and requires a later ADR with coherent integrity and recovery
  semantics before it may be enabled.
- Sidecar updates and paired PDF/sidecar moves are atomic or recoverably journaled.
- SQLite contains no state that cannot be recovered from authoritative artifacts.
- Logs exclude OCR text, complete model responses, extracted values, and document
  paths by default.
- New lifecycle timestamps use timezone-aware UTC in one canonical serialized
  format; legacy naive timestamps remain explicitly timezone-unknown until
  migrated from reliable external evidence.
- Planned interfaces do not become alternative sources of business logic.

## Quality attributes

| Attribute | Architectural response |
|---|---|
| Security | Exact-root confinement, local-first inference, hostile-input handling, explicit writes. |
| Privacy | Data minimization, local defaults, sensitive-artifact guidance, redacted telemetry. |
| Reliability | Atomic writes, recoverable commands, collision handling, stable IDs. |
| Explainability | Sidecars, evidence references, warnings, confidence, human review. |
| Portability | Python core, `pathlib`, no operating-system-specific core workflow. |
| Maintainability | Shared application services, typed models, ADRs, synchronized documentation. |
| Performance | Bounded parsing/inference, page-aware processing, resumable derived indexes. |

## Near-term decisions and delivery order

1. Adopt this documentation baseline and synchronize it with the implementation.
2. Fix pre-read source validation, exact-root validation, and deterministic path
   derivation.
3. Define atomic sidecar writes and recoverable paired moves.
4. Remove or keep disabled PDF metadata rewriting for the first release.
5. Add PDF resource limits and enforce local-only inference for the first release.
6. Implement and test SQLite rebuild before calling the index recoverable.
7. Introduce provider-neutral inference while retaining the local-only default.
8. Add page-level evidence and hierarchical long-document processing.
9. Connect the review workbench through application services.
10. Add MCP read tools, then separately review any proposed write tools.
11. Introduce canonical timezone-aware UTC lifecycle timestamps and migrate or
    explicitly mark legacy naive sidecar values without guessing their offset.

Remote inference is outside the first-release boundary. Bulk approval, including
`approve --all`, must display the exact item count and require an additional
confirmation before any move begins.

Older branches and draft work must be reconciled before new layers are presented
as current functionality.

## Diagram maintenance

Named Mermaid blocks in this file and the threat model are the editable diagram
source. The renderer must discover each `<!-- diagram: name -->` block, validate it with a pinned
Mermaid version, and publish SVG artifacts in CI. Generated renderings are not
committed.

Update the relevant diagram whenever a component, trust boundary, persistence
mechanism, inference provider, interface, data authority, or material data flow
changes. The same pull request must update this document and the threat model
when their claims are affected.

Third-party CI actions are pinned to reviewed, immutable commit SHAs. Update
them only in a scoped dependency pull request that records the corresponding
upstream release or tag, verifies the selected commit SHA, and successfully
renders the diagrams before merge.

Mermaid CLI is a development-only CI dependency declared at an exact version in
`package.json`; `package-lock.json` pins its transitive graph and integrity
hashes, and CI installs that graph with `npm ci`. The renderer invokes only the
installed local binary by its explicit platform-specific path and fails when it
is absent, so rendering cannot resolve npm packages dynamically.
CI installs the Chrome Headless Shell revision selected by the lockfile-pinned
Puppeteer version before rendering.
Mermaid launches a browser and parses maintained diagram source, making updates
security-relevant: each scoped dependency pull request must review the lockfile
diff and `npm audit`, record the upstream release and security impact, and render
all diagrams successfully before merge. Version 11.17.0 replaces 11.12.0 because
the latter's locked Puppeteer graph contained high-severity `extract-zip`
advisories.
