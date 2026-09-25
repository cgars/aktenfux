# Aktenfux agent working agreement

This file defines how coding agents and human contributors should change Aktenfux. Protect user documents first; optimize convenience second.

## Start here

Before changing behavior, read:

1. `docs/architecture.md`
2. `docs/threat-model.md`
3. `docs/eli5.md`
4. applicable records under `docs/decisions/`
5. `SECURITY.md`

Inspect the implementation and tests before assuming a documented control already exists. Documentation must distinguish **current**, **planned**, and **required before release** behavior.

## Non-negotiable invariants

- A user document is never silently overwritten or deleted.
- Source and destination containment is validated before document content is read or written.
- Model output is untrusted data, never a command or filesystem capability.
- A human explicitly approves consequential changes.
- The sidecar is authoritative Aktenfux metadata; SQLite is derived and rebuildable.
- A PDF and its sidecar remain a recoverable pair across interruption or failure.
- Dry-run does not mutate document, sidecar, archive, or index state.
- Sensitive document content, model payloads, secrets, and local paths do not enter logs or fixtures by default.
- CLI, future UI, and future MCP interfaces use the same application services and policy checks.

If a requested change conflicts with an invariant, stop and propose an ADR instead of working around it.

## Delivery order

1. State the user-visible behavior and safety boundary.
2. Update or add an ADR when the change alters an architectural decision.
3. Add tests for failure, interruption, and adversarial input before or with implementation.
4. Implement the smallest coherent change through typed application boundaries.
5. Update architecture, threat model, ELI5, and operational documentation when affected.
6. Perform a fresh reviewer pass over the complete diff: trace alternate execution
   paths and verify every absolute current-state claim against code and tests.
7. Run verification and report what was and was not exercised.

### Path-sensitive effect review

Before claiming that a command is read-only, mutation-free, offline, or that an
effect occurred, build an effect ledger from the implementation rather than
from the command's intended purpose:

1. Start at each CLI entry point and follow every helper it can call.
2. Record filesystem reads, existence checks, directory creation, writes,
   moves, database connections, network calls, external-service storage, and
   terminal/log output in execution order.
3. Split the ledger at configuration branches, confirmations, exceptions, and
   every early return; do not merge `--dry-run` and normal paths or commands
   that merely share a helper.
4. Mark each effect as unconditional, conditional, or impossible on that path.
5. Verify claims against focused tests. A completion message may say an effect
   happened only when the application returns or observes evidence for it;
   otherwise describe it as a possible hazard.

Perform the same exercise once from the opposite direction: select each writer,
database connector, network client, and subprocess boundary and identify every
entry point that can reach it. Reconcile that reverse inventory with the
command ledger before pushing.

## Engineering rules

- Use `pathlib` and resolve paths deliberately; test traversal, absolute paths, symlinks, case behavior, and cross-platform separators.
- Use typed models at external boundaries. Validate length, character set, enum values, and schema version before use.
- Keep filesystem actions out of prompt construction and model parsing.
- Write sidecars atomically. Design multi-file moves with staging or a journal plus idempotent recovery.
- Use timezone-aware UTC timestamps serialized consistently.
- Avoid broad exception handling that hides partial state. Errors must say what remains safe and how recovery proceeds.
- Do not log raw extracted text, full prompts, raw model responses, secrets, or complete user paths by default.
- Do not add a dependency without a documented purpose, maintenance assessment, and security impact.
- Keep behavior portable across supported operating systems; do not assume POSIX-only path semantics.
- Use synthetic fixtures. Never commit real documents, sidecars, databases, caches, model downloads, or runtime artifacts.
- Preserve user changes and keep commits narrowly scoped.

## Required verification

Run the repository's complete test suite, currently `pytest`, plus focused tests for changed behavior. For documentation changes:

- render or lint Mermaid diagrams;
- verify relative links;
- check that the architecture describes actual behavior accurately;
- update threat entries and release gates when trust boundaries change.

If a check cannot run, state the exact reason and remaining risk in the pull request.

## Architecture and security maintenance

Update `docs/architecture.md` when components, data flows, trust boundaries, authoritative stores, or external interfaces change. Update `docs/threat-model.md` when data, permissions, dependencies, network exposure, write capabilities, or failure modes change. Add an ELI5 explanation for concepts a document owner must understand.

Diagrams are Mermaid source in Markdown. CI may generate SVG artifacts for review or publication; generated binary diagrams are not committed unless a later ADR changes this policy.

## Interface rules

New CLI, UI, or MCP operations must identify whether they are read-only or mutating. Mutating operations need narrow typed inputs, a preview, explicit confirmation, idempotency or recovery behavior, and an audit-safe result. Do not expose arbitrary shell execution or unchecked paths.

MCP work begins with read-only inspection tools. Write tools require a dedicated security design and release gate.

## Pull requests

Use the repository template. Explicitly report architecture, threat-model, diagram, data-lifecycle, privacy, release-gate, and ADR impact. “No impact” requires a short explanation, not an unchecked assumption.
