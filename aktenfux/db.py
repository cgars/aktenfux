"""Optional SQLite index for document status, history, and duplicate detection."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from aktenfux.schema import SidecarDocument

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id              TEXT PRIMARY KEY,
    original_path   TEXT,
    current_path    TEXT,
    sha256          TEXT UNIQUE,
    status          TEXT,
    suggested_folder    TEXT,
    suggested_filename  TEXT,
    summary_short   TEXT,
    action_required INTEGER,
    deadline        TEXT,
    confidence      REAL,
    model           TEXT,
    processed_at    TEXT,
    approved_at     TEXT,
    error_message   TEXT
);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def initialize_db(db_path: Path) -> None:
    """Create the database and schema if they don't exist."""
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)
    logger.debug("SQLite database initialized at %s", db_path)


def upsert_document(db_path: Path, sidecar: SidecarDocument) -> None:
    """Insert or replace a document record."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO documents (
                id, original_path, current_path, sha256, status,
                suggested_folder, suggested_filename, summary_short,
                action_required, deadline, confidence, model,
                processed_at, approved_at, error_message
            ) VALUES (
                :id, :original_path, :current_path, :sha256, :status,
                :suggested_folder, :suggested_filename, :summary_short,
                :action_required, :deadline, :confidence, :model,
                :processed_at, :approved_at, :error_message
            )
            ON CONFLICT(id) DO UPDATE SET
                current_path = excluded.current_path,
                sha256 = excluded.sha256,
                status = excluded.status,
                suggested_folder = excluded.suggested_folder,
                suggested_filename = excluded.suggested_filename,
                summary_short = excluded.summary_short,
                action_required = excluded.action_required,
                deadline = excluded.deadline,
                confidence = excluded.confidence,
                model = excluded.model,
                processed_at = excluded.processed_at,
                approved_at = excluded.approved_at,
                error_message = excluded.error_message
            """,
            {
                "id": sidecar.id,
                "original_path": sidecar.original_path,
                "current_path": sidecar.current_path,
                "sha256": sidecar.sha256,
                "status": sidecar.status,
                "suggested_folder": sidecar.suggested_folder,
                "suggested_filename": sidecar.suggested_filename,
                "summary_short": sidecar.summary_short,
                "action_required": int(sidecar.action_required),
                "deadline": sidecar.deadline,
                "confidence": sidecar.confidence,
                "model": sidecar.model,
                "processed_at": sidecar.processed_at,
                "approved_at": sidecar.approved_at,
                "error_message": sidecar.error_message,
            },
        )


def get_document(db_path: Path, doc_id: str) -> sqlite3.Row | None:
    """Fetch a document row by ID."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
    return row


def find_by_sha256(db_path: Path, sha256: str) -> sqlite3.Row | None:
    """Return an existing document with the same SHA-256 (duplicate check)."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE sha256 = ?", (sha256,)
        ).fetchone()
    return row


def count_by_status(db_path: Path) -> dict[str, int]:
    """Return a dict of status → count for all documents."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM documents GROUP BY status"
        ).fetchall()
    return {row["status"]: row["n"] for row in rows}


def update_status(
    db_path: Path,
    doc_id: str,
    status: str,
    *,
    current_path: str | None = None,
    approved_at: str | None = None,
    error_message: str | None = None,
) -> None:
    """Update the status (and optionally path/timestamps) of a document."""
    updates = ["status = :status"]
    params: dict = {"id": doc_id, "status": status}
    if current_path is not None:
        updates.append("current_path = :current_path")
        params["current_path"] = current_path
    if approved_at is not None:
        updates.append("approved_at = :approved_at")
        params["approved_at"] = approved_at
    if error_message is not None:
        updates.append("error_message = :error_message")
        params["error_message"] = error_message

    sql = (  # noqa: S608
        # The `updates` list is built entirely from controlled string literals
        # (column names) and bound parameters (:name), never from user input.
        f"UPDATE documents SET {', '.join(updates)} WHERE id = :id"
    )
    with _connect(db_path) as conn:
        conn.execute(sql, params)
