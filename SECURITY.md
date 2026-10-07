# Security Policy

Aktenfux processes documents that may contain personal, financial, legal, or medical information. Treat PDFs, extracted text, model responses, sidecars, databases, filenames, and logs as sensitive.

## Project status

Aktenfux is pre-release software. There is currently no supported production version and no claim that the application is suitable for unattended processing of sensitive archives. Planned controls in the architecture and threat model are release requirements, not statements that the controls already exist.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting feature for the repository. If that feature is unavailable, contact the maintainers through a private channel listed on the repository profile. Do not open a public issue containing a working exploit, private document, extracted text, credential, filesystem layout, sidecar, database, or sensitive log.

Include, when safe:

- affected commit or version;
- operating system and relevant dependency versions;
- a minimal reproduction using synthetic data;
- expected and observed behavior;
- likely impact;
- suggested mitigation, if known.

Maintainers should acknowledge the report, coordinate reproduction and remediation privately, and publish an advisory when users need to act. No response-time guarantee is made while the project is pre-release.

## Please use synthetic evidence

Never upload a real personal document to demonstrate a problem. Create a minimal synthetic PDF and remove usernames, directory names, tokens, model responses, and document-derived metadata from diagnostic output.

## Security-sensitive areas

Reports are especially useful for:

- source or destination path escape, including symbolic links;
- unsafe PDF parsing, OCR, or metadata rewriting;
- prompt injection that becomes an application action;
- untrusted model fields used as paths or commands;
- unintended remote inference or network exposure;
- partial writes, inconsistent PDF/sidecar pairs, or unrecoverable moves;
- stale or misleading integrity hashes;
- sidecar or SQLite tampering and unsafe rebuild behavior;
- secret or document-content leakage through logs, fixtures, caches, or CI artifacts;
- authorization or confirmation bypasses in future UI or MCP interfaces;
- vulnerable or compromised dependencies.

## Intended deployment boundary

The intended default is a single-user, local-first application with local inference. This is not a multi-user authorization boundary. Exposing model services, future UI endpoints, or a future MCP server beyond the local machine changes the threat model and requires an explicit security design.

## Maintainer references

- [Architecture](docs/architecture.md)
- [Threat model](docs/threat-model.md)
- [ELI5 guide](docs/eli5.md)
- [ADR 0001](docs/decisions/0001-local-first-human-controlled-archive.md)

