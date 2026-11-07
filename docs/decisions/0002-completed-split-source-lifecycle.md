# ADR 0002: Completed split sources move from `_Split` to `_Imported`

**Status:** Accepted
**Date:** 2026-11-07
**Decision owners:** Aktenfux maintainers

## Context

`_Split` currently serves two different roles:

1. active queue for split planning/suggestion/execution;
2. long-term retention for already completed split sources.

That conflation keeps completed sources visible as if they were still actionable.
It also allows accidental alternate split plans against already completed sources.

## Alternatives considered

### 1) Activate `_Imported` for retained completed split sources (**chosen**)

- Keeps active queue and retained history in distinct lifecycle roots.
- Reuses existing configured lifecycle root with no new top-level directory.
- Gives operators one predictable retained location for backup/restore and
  reconciliation.

### 2) New `_SplitCompleted` root

- Explicitly names the lifecycle but adds another configured root and migration
  burden.
- Rejected for now to keep configuration and operator workflows smaller.

### 3) Completed sub-area beneath `_Split`

- Keeps files near split work but risks eligibility drift and accidental
  inclusion from recursive scans/tools.
- Rejected because issue #26 requires explicit lifecycle separation that is not
  a frontend-only filter.

## Decision

After durable split completion, Aktenfux transitions the original source PDF
and authoritative sidecar from active `_Split` to retained `_Imported` using a
durable transition journal and staged publication protocol.

`_Split` remains active work only. `_Imported` is the retained completed-source
lifecycle root.

### Lifecycle semantics

- **Active (`_Split`)**: eligible for suggest/plan/preview/execute.
- **In-progress/recoverable**: resume only the same compatible operation.
- **Completed (`_Imported`)**: hidden from active queue; new/alternate plans and
  suggestions are rejected as already completed.
- **Exact completed retry**: the same operation ID returns/validates the
  recorded result and never republishes consumed outputs.

### Transition protocol (implemented)

Per operation, Aktenfux writes a transition journal under:

`_Imported/.split-completed-transition/<operation-id>/transition-state.json`

Then it advances recoverably through:

1. `planned` – journal written with source/destination identities.
2. `pdf_staged` – source PDF staged in operation staging.
3. `sidecar_staged` – source sidecar staged in operation staging.
4. `retained_pdf_published` – retained PDF published with exclusive no-overwrite semantics.
5. `retained_sidecar_published` – retained sidecar published with exclusive no-overwrite semantics.
6. `source_pdf_removed` – source PDF cleanup completed.
7. `source_sidecar_removed` – source sidecar cleanup completed.
8. `finalized` – journal finalized and operation staging cleaned.

Recovery may resume from any state above. Unknown/ambiguous state fails closed.

### Identity, naming, collisions

- Destination naming is deterministic:
  - `<stem>--completed-<operation-id-prefix>.pdf`
  - `<stem>--completed-<operation-id-prefix>.json`
- Source hash and sidecar provenance identity are revalidated.
- Retained publication is exclusive/no-overwrite and collision-safe for same and
  concurrent attempts.

### Discoverability

- Read-only retained inspection: `afu split-completed`.

### Migration

- Dry-run-first reconciliation command:
  `afu split-reconcile-completed [--apply] [--yes]`
- Dry-run reports eligible candidates plus ambiguous/colliding/mismatched or
  partial states (including journal-discovered partial transitions).
- `--apply` mutates only explicitly reconcilable candidates; reruns are
  idempotent.

### Backup/restore and operator recovery

- Backup/restore must include `_Split`, `_Imported`, and
  `_Imported/.split-completed-transition`.
- If reconciliation reports issues, operators should inspect reported source,
  retained, and transition-journal paths and rerun dry-run after correction.
- Split execution and reconciliation both fail closed instead of inferring
  success from partial artifacts.

## Consequences

### Benefits

- `_Split` now models active work only.
- Completed sources remain recoverable with provenance preserved.
- CLI and workbench share the same eligibility behavior through split services.

### Trade-offs

- Added lifecycle transition and reconciliation complexity.
- Operators must use completed-source inspection/reconciliation workflows.

## ADR relationship

This ADR refines ADR 0001's recoverable lifecycle and shared-service rules
without replacing them.
