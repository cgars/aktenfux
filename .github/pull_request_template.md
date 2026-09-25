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
- [ ] A path-sensitive effect ledger traces every changed CLI path through preflight calls, helper calls, conditionals, confirmations, exceptions, and early returns.
- [ ] The reverse effect inventory maps every changed filesystem writer, database connector, network client, external-service write, and subprocess boundary back to all reachable commands.
- [ ] Every dry-run-capable command was checked separately against its documented filesystem, database, network, external-service, index, and lifecycle read/write/move contract.
- [ ] User-visible completion messages distinguish observed effects from conditional or merely possible effects.
- [ ] Documentation matches implemented behavior and labels planned behavior clearly.
- [ ] A fresh full-diff reviewer pass checked absolute current-state claims and alternate execution paths.
- [ ] Mermaid diagrams render and documentation links resolve.
- [ ] Threats and mitigations were updated when the attack surface changed.
- [ ] No real documents, private data, secrets, model payloads, local paths, databases, caches, or generated runtime artifacts were committed.
- [ ] New dependencies have a documented purpose and security assessment.

## Evidence and limitations

<!-- List commands run and results. State anything not tested and the remaining risk. Use synthetic data only. -->
