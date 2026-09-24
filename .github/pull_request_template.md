## What changed?

<!-- Describe the smallest coherent change. -->

## Why?

<!-- Link the issue, requirement, threat, or decision. -->

## ELI5

<!-- Explain the change to a document owner without implementation jargon. -->

## Safety invariant

<!-- Which invariant is preserved or strengthened? If one changes, link the ADR. -->

## User and data impact

- User-visible behavior:
- Data read:
- Data written or moved:
- Failure and recovery behavior:
- Compatibility or migration impact:

## Design impact

For each item, describe the impact or explain why there is none.

- Architecture:
- Threat model:
- Trust boundaries or permissions:
- Diagrams:
- Sidecar schema or data lifecycle:
- Privacy or logging:
- Release gates:
- ADRs:

## Verification

- [ ] Full `pytest` suite passes.
- [ ] Focused success, failure, adversarial-input, and interruption tests were added or updated.
- [ ] Dry-run remains free of document-state mutations.
- [ ] Documentation matches implemented behavior and labels planned behavior clearly.
- [ ] A fresh full-diff reviewer pass checked absolute current-state claims and alternate execution paths.
- [ ] Mermaid diagrams render and documentation links resolve.
- [ ] Threats and mitigations were updated when the attack surface changed.
- [ ] No real documents, private data, secrets, model payloads, local paths, databases, caches, or generated runtime artifacts were committed.
- [ ] New dependencies have a documented purpose and security assessment.

## Evidence and limitations

<!-- List commands run and results. State anything not tested and the remaining risk. Use synthetic data only. -->
