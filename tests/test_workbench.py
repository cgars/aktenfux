"""Focused tests for the local split workbench API."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from aktenfux.workbench import _validate_loopback_host, create_app
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


def test_workbench_rejects_root_escape_and_unexpected_ids(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docbench000002", pages=8)
    app = create_app(cfg)
    client = TestClient(app)

    response = client.get("/api/split/documents/../../etc/passwd/pdf")
    assert response.status_code in {400, 404}

    response = client.get("/api/split/documents/../bundle/pdf")
    assert response.status_code in {400, 404}

    response = client.get(f"/api/split/documents/{'a' * 300}/pdf")
    assert response.status_code in {400, 404}

    response = client.get("/api/split/documents/docbench000999/pdf")
    assert response.status_code == 404

    invalid_cfg = make_config(tmp_path / "external-root")
    invalid_cfg.base_dir = (tmp_path / "outside-base").resolve()
    invalid_cfg.split_dir = str((tmp_path / "outside-base" / "_Split").resolve())
    invalid_app = create_app(invalid_cfg)
    invalid_client = TestClient(invalid_app)
    invalid_response = invalid_client.get("/api/split/documents")
    assert invalid_response.status_code == 500


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

    app.state.preview_store["docbench000002"] = {"token": token, "plan": plan, "completed": True}
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


def test_workbench_validates_loopback_binding() -> None:
    assert _validate_loopback_host("127.0.0.1") == "127.0.0.1"
    assert _validate_loopback_host("localhost") == "localhost"
    try:
        _validate_loopback_host("0.0.0.0")
    except ValueError:
        pass
    else:
        raise AssertionError("Expected loopback-only hosts to reject 0.0.0.0")
