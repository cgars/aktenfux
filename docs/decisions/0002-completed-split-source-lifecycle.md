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

## Decision

After durable split completion, Aktenfux moves the original source PDF and its
authoritative sidecar together from `_Split` into `_Imported`.

`_Split` remains the active queue only. `_Imported` becomes the explicit
retained-completed lifecycle root for split sources.

### Lifecycle semantics

- **Active (`_Split`)**: eligible for suggest/plan/preview/execute.
- **In-progress/recoverable**: resume only the same compatible operation.
- **Completed (`_Imported`)**: hidden from active queue; new/alternate plans and
  suggestions are rejected as already completed.
- **Exact completed retry**: the same operation ID returns/validates the
  recorded result and never republishes consumed outputs.

### Transition protocol

- Transition occurs only after output publication and completed provenance are durable.
- The source PDF/sidecar move as a recoverable pair with a transition journal.
- Destination names are deterministic, collision-resistant, and never overwrite.
- Source and destination roots, containment, and regular-file identity are
  validated before effects.
- Recovery is idempotent after each individual effect.

### Discoverability

- Add a read-only CLI listing for retained completed split sources in `_Imported`.

### Migration

- Add a dry-run-first reconciliation command that identifies completed sidecar
  pairs still in `_Split`, reports ambiguous/colliding/mismatched states, and
  moves only explicitly confirmed safe candidates.

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
