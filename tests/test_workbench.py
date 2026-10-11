"""Focused tests for the local split workbench API."""
from __future__ import annotations

import threading

from fastapi.testclient import TestClient

import aktenfux.workbench as workbench_module
from aktenfux.workbench import create_app
from tests.split_helpers import make_config, write_split_doc


def test_workbench_lists_only_split_documents_and_serves_pdf(tmp_path):
    cfg = make_config(tmp_path)
    pdf = write_split_doc(cfg.split_path, "bundle.pdf", "docbench000001", pages=6)
    app = create_app(cfg)
    client = TestClient(app)

    response = client.get("/api/split/documents")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [entry["id"] for entry in payload] == ["docbench000001"]
    assert payload[0]["label"] == "bundle.pdf"

    pdf_response = client.get("/api/split/documents/docbench000001/pdf")
    assert pdf_response.status_code == 200, pdf_response.text
    assert pdf_response.headers["content-type"].startswith("application/pdf")
    assert pdf_response.content.startswith(b"%PDF")
    assert pdf.read_bytes() == pdf_response.content


def test_workbench_preview_requires_latest_plan_and_prevents_duplicate_execute(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docbench000002", pages=8)
    app = create_app(cfg)
    client = TestClient(app)

    preview = client.post(
        "/api/split/preview",
        json={"document_id": "docbench000002", "boundaries": [4]},
    )
    assert preview.status_code == 200, preview.text
    token = preview.json()["preview_token"]
    plan = preview.json()["plan"]

    stale = client.post(
        "/api/split/execute",
        json={
            "document_id": "docbench000002",
            "preview_token": "0" * 64,
            "plan": plan,
        },
    )
    assert stale.status_code == 409, stale.text
    assert "stale" in stale.json()["detail"].lower()

    execution = client.post(
        "/api/split/execute",
        json={
            "document_id": "docbench000002",
            "preview_token": token,
            "plan": plan,
        },
    )
    assert execution.status_code == 200, execution.text
    outputs = execution.json()["result"]["outputs"]
    assert [item["filename"] for item in outputs] == ["bundle--part-01.pdf", "bundle--part-02.pdf"]
    assert (cfg.inbox_path / "bundle--part-01.pdf").exists()
    assert (cfg.inbox_path / "bundle--part-02.pdf").exists()

    app.state.preview_store["docbench000002"] = {"token": token, "plan": plan}
    duplicate = client.post(
        "/api/split/execute",
        json={
            "document_id": "docbench000002",
            "preview_token": token,
            "plan": plan,
        },
    )
    assert duplicate.status_code == 409, duplicate.text
    assert "already completed" in duplicate.json()["detail"].lower()

    blocked = client.post(
        "/api/split/execute",
        json={
            "document_id": "docbench000002",
            "preview_token": token,
            "plan": plan,
        },
    )
    assert blocked.status_code == 409, blocked.text
    assert "already completed" in blocked.json()["detail"].lower()
