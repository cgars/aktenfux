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

    response = client.get("/api/split/documents/%2e%2e/%2e%2e/etc/passwd/pdf")
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
            "confirm": True,
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
            "confirm": True,
        },
    )
    assert duplicate.status_code == 409, duplicate.text
    assert "already completed" in duplicate.json()["detail"].lower()

    stale_token = client.post(
        "/api/split/preview",
        json={"document_id": "docbench000002", "boundaries": [2]},
    )
    assert stale_token.status_code == 200, stale_token.text
    replacement = stale_token.json()
    valid_new_token = replacement["preview_token"]
    invalidated = client.post(
        "/api/split/execute",
        json={
            "document_id": "docbench000002",
            "preview_token": valid_new_token,
            "plan": plan,
            "confirm": True,
        },
    )
    assert invalidated.status_code == 409, invalidated.text
    assert "exact most recently previewed" in invalidated.json()["detail"].lower()


def test_workbench_requires_explicit_confirmation_and_rejects_null_origin(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docbench000003", pages=8)
    app = create_app(cfg)
    client = TestClient(app)

    preview = client.post(
        "/api/split/preview",
        json={"document_id": "docbench000003", "boundaries": [4]},
    )
    assert preview.status_code == 200, preview.text
    payload = preview.json()

    direct = client.post(
        "/api/split/execute",
        json={
            "document_id": "docbench000003",
            "preview_token": payload["preview_token"],
            "plan": payload["plan"],
        },
    )
    assert direct.status_code == 409, direct.text
    assert "confirmation" in direct.json()["detail"].lower()

    null_origin = TestClient(app, headers={"Origin": "null"})
    blocked = null_origin.post(
        "/api/split/execute",
        json={
            "document_id": "docbench000003",
            "preview_token": payload["preview_token"],
            "plan": payload["plan"],
            "confirm": True,
        },
    )
    assert blocked.status_code == 403, blocked.text

    same_origin = client.post(
        "/api/split/execute",
        json={
            "document_id": "docbench000003",
            "preview_token": payload["preview_token"],
            "plan": payload["plan"],
            "confirm": True,
        },
    )
    assert same_origin.status_code == 200, same_origin.text


def test_workbench_cleans_up_inflight_state_after_runtime_failure(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docbench000004", pages=8)
    app = create_app(cfg)
    client = TestClient(app)

    preview = client.post(
        "/api/split/preview",
        json={"document_id": "docbench000004", "boundaries": [4]},
    )
    assert preview.status_code == 200, preview.text
    payload = preview.json()

    def fail_execute(plan, config):
        raise RuntimeError("boom")

    monkeypatch.setattr("aktenfux.workbench.execute_split", fail_execute)

    failed = client.post(
        "/api/split/execute",
        json={
            "document_id": "docbench000004",
            "preview_token": payload["preview_token"],
            "plan": payload["plan"],
            "confirm": True,
        },
    )
    assert failed.status_code == 500, failed.text
    assert not app.state.in_flight


def test_workbench_rejects_oversized_and_unexpected_document_ids(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docbench000005", pages=8)
    app = create_app(cfg)
    client = TestClient(app)

    oversized = client.post(
        "/api/split/preview",
        content=("{" + '"document_id": "x"' + ',"boundaries": [2]}') * 100000,
        headers={"Content-Type": "application/json"},
    )
    assert oversized.status_code == 413

    unicode_route = client.get("/api/split/documents/é/pdf")
    assert unicode_route.status_code == 400

    large_route = client.get(f"/api/split/documents/{'a' * 300}/pdf")
    assert large_route.status_code == 400

    encoded_route = client.get("/api/split/documents/%2e%2e/%2e%2e/etc/passwd/pdf")
    assert encoded_route.status_code == 400


def test_workbench_validates_loopback_binding() -> None:
    assert _validate_loopback_host("127.0.0.1") == "127.0.0.1"
    assert _validate_loopback_host("localhost") == "localhost"
    try:
        _validate_loopback_host("0.0.0.0")
    except ValueError:
        pass
    else:
        raise AssertionError("Expected loopback-only hosts to reject 0.0.0.0")
