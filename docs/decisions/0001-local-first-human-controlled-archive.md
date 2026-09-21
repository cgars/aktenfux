# ADR 0001: Local-first, human-controlled archive with authoritative sidecars

**Status:** Accepted  
**Date:** 2026-09-21  
**Decision owners:** Aktenfux maintainers

## Context

Aktenfux extracts text from personal documents, asks a language model for metadata suggestions, creates sidecars, and helps a user file the documents. The documents can contain highly sensitive information. Model output is probabilistic and untrusted, while filesystem changes can be difficult to reverse.

The project also anticipates additional interfaces such as a graphical UI and an MCP server. Without a shared application boundary, those interfaces could implement different safety rules and produce incompatible state.

## ELI5

The PDF is the original letter, its JSON sidecar is the index card, and SQLite is a catalogue copied from the cards. A reading assistant can suggest what the card should say, but the clerk and the archive owner decide where the letter goes.

## Decision

Aktenfux will use the following architectural rules:

1. Processing is local-first. The inference endpoint must be restricted to the local machine for the first release. Remote inference is unsupported until a later ADR, explicit opt-in design, and security review permit it.
2. The user retains authority over the original PDF. Aktenfux does not silently overwrite or delete source material.
3. The JSON sidecar is the authoritative Aktenfux metadata record. SQLite is a derived, rebuildable search index.
4. Language-model output is an untrusted proposal. It cannot directly choose filesystem paths, execute commands, or authorize a state change.
5. Consequential operations use preview and explicit human approval. Bulk approval such as `approve --all` displays the exact scope and requires an additional confirmation. Low-confidence or incomplete-evidence results go to review.
6. CLI, future UI, and future MCP tools call the same typed application services and policy checks.
7. Mutations of a PDF and its sidecar are designed as recoverable operations, with atomic writes where supported and clear recovery state where a multi-file operation cannot be atomic.
8. Original PDF bytes remain unchanged by default. PDF metadata rewriting is unsupported for the first release and requires a later ADR before it may be enabled.

## Consequences

### Benefits

- Sensitive content stays within a small, understandable trust boundary by default.
- A damaged or deleted SQLite index can be rebuilt from sidecars.
- Every interface gets consistent validation and approval rules.
- Model mistakes are less likely to become filesystem actions.
- Provenance and recovery are easier to explain and test.

### Costs and trade-offs

- Sidecar schema evolution and index rebuilding require explicit tooling.
- Human review reduces unattended throughput.
- Safe paired-file operations need journals, staging, or recovery logic.
- Restricting model-selected paths requires deterministic mapping code.
- Remote inference cannot be enabled as a casual endpoint change and is unsupported for the first release.

## Alternatives considered

### SQLite as the source of truth

Rejected because it makes portable document-plus-metadata pairs harder to preserve and increases recovery dependence on one database.

### Model-generated destination paths

Rejected because natural-language output is not a safe filesystem capability. Semantic suggestions must be mapped by trusted code.

### Fully automatic filing by default

Rejected because document extraction, classification, dates, and paths can be wrong. Explicit approval is appropriate for consequential changes.

### Separate business logic in every interface

Rejected because safety behavior would drift between CLI, UI, and MCP.

## Security implications

This decision reduces, but does not remove, risk. Path containment must be checked before reading and writing, symbolic links require explicit handling, PDFs and OCR engines remain attack surfaces, logs may leak content, and a local service can still be exposed through misconfiguration. The threat model defines the controls and release gates.

## Verification

- Tests prove that untrusted model fields cannot escape configured roots.
- Tests rebuild SQLite from sidecars and compare observable search results.
- Dry-run tests prove that no document state is mutated.
- Integration tests exercise interrupted paired-file operations and recovery.
- Each interface passes the same application-service contract tests.
- Security tests cover loopback-only defaults and explicit remote-inference configuration.

## Follow-up decisions

Separate ADRs should cover:

- sidecar schema versioning and migrations;
- transaction and recovery design for PDF/sidecar operations;
- remote inference policy;
- MCP authentication, tool permissions, and confirmation protocol;
- policy for rewriting PDF metadata.

## Supersession

If this decision changes, a new ADR must explain the replacement and link back to this record. Do not silently rewrite an accepted architectural decision.
