"""Security and failure tests for the local split workbench."""
from __future__ import annotations

import asyncio
import json
import re
import pytest
from fastapi.testclient import TestClient

import aktenfux.workbench as workbench_module
from aktenfux.workbench import (
    MAX_REQUEST_BODY_BYTES,
    _validate_loopback_host,
    create_app,
)
from tests.split_helpers import make_config, write_split_doc

BASE_URL = "http://127.0.0.1:8000"
TOKEN_PATTERN = re.compile(r'name="aktenfux-csrf-token" content="([^"]+)"')


def browser_client(app, *, raise_server_exceptions: bool = True) -> TestClient:
    client = TestClient(
        app,
        base_url=BASE_URL,
        raise_server_exceptions=raise_server_exceptions,
    )
    root = client.get("/")
    assert root.status_code == 200
    match = TOKEN_PATTERN.search(root.text)
    assert match is not None
    client.headers.update(
        {
            "Origin": BASE_URL,
            "X-Aktenfux-CSRF": match.group(1),
        }
    )
    return client


def preview_document(client: TestClient, document_id: str, boundaries=(4,)) -> dict:
    response = client.post(
        "/api/split/preview",
        json={"document_id": document_id, "boundaries": list(boundaries)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def execute_payload(document_id: str, preview: dict, **overrides) -> dict:
    payload = {
        "document_id": document_id,
        "preview_token": preview["preview_token"],
        "plan": preview["plan"],
        "confirm": True,
    }
    payload.update(overrides)
    return payload


def test_workbench_page_serves_security_headers_and_external_javascript(tmp_path):
    cfg = make_config(tmp_path)
    cfg.split_path.mkdir(parents=True)
    app = create_app(cfg)
    client = TestClient(app, base_url=BASE_URL)

    response = client.get("/")

    assert response.status_code == 200
    assert TOKEN_PATTERN.search(response.text)
    assert '<script src="/workbench.js" defer></script>' in response.text
    assert 'id="refresh-preview"' in response.text
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    javascript = client.get("/workbench.js")
    assert javascript.status_code == 200
    assert javascript.headers["content-type"].startswith("application/javascript")
    assert "innerHTML" not in javascript.text


def test_workbench_lists_only_valid_split_documents_and_serves_pdf(tmp_path):
    cfg = make_config(tmp_path)
    pdf = write_split_doc(cfg.split_path, "bundle.pdf", "docbench000001", pages=6)
    (cfg.split_path / "broken.pdf").write_bytes(b"not a pdf")
    app = create_app(cfg)
    client = browser_client(app)

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


def test_workbench_bounds_and_text_sanitizes_display_names(tmp_path):
    cfg = make_config(tmp_path)
    pdf = write_split_doc(cfg.split_path, "bundle.pdf", "docdisplay001", pages=2)
    sidecar = pdf.with_suffix(".json")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["suggested_filename"] = "<img src=x onerror=alert(1)>\x1b" + ("x" * 500)
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    client = browser_client(create_app(cfg))

    label = client.get("/api/split/documents").json()[0]["label"]

    assert label.startswith("<img src=x onerror=alert(1)>�")
    assert len(label) == 200


def test_workbench_rejects_external_root_on_list_and_direct_pdf(tmp_path):
    cfg = make_config(tmp_path / "base")
    external = tmp_path / "external" / "_Split"
    external.mkdir(parents=True)
    write_split_doc(external, "outside.pdf", "docexternal001", pages=2)
    cfg.split_dir = str(external.resolve())
    client = browser_client(create_app(cfg))

    assert client.get("/api/split/documents").status_code == 500
    direct = client.get("/api/split/documents/docexternal001/pdf")
    assert direct.status_code == 404
    assert "direct child" in direct.json()["detail"]


def test_workbench_rejects_symlink_pdf(tmp_path):
    cfg = make_config(tmp_path)
    pdf = write_split_doc(cfg.split_path, "bundle.pdf", "docsymlink001", pages=2)
    external = tmp_path / "external.pdf"
    external.write_bytes(pdf.read_bytes())
    pdf.unlink()
    try:
        pdf.symlink_to(external)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    client = browser_client(create_app(cfg))

    assert client.get("/api/split/documents").json() == []
    assert client.get("/api/split/documents/docsymlink001/pdf").status_code == 404


@pytest.mark.parametrize(
    "document_id",
    ["é", "１２３", "a" * 65, "../bundle", "doc/id", ""],
)
def test_workbench_rejects_unexpected_document_ids(tmp_path, document_id):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docbench000002", pages=8)
    client = browser_client(create_app(cfg))

    route_id = document_id or "%00"
    route = client.get(f"/api/split/documents/{route_id}/pdf")
    assert route.status_code in {400, 404}

    preview = client.post(
        "/api/split/preview",
        json={"document_id": document_id, "boundaries": [4]},
    )
    assert preview.status_code == 422


def test_workbench_rejects_encoded_traversal(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docbench000003", pages=8)
    client = browser_client(create_app(cfg))

    for path in (
        "/api/split/documents/../../etc/passwd/pdf",
        "/api/split/documents/%2e%2e/%2e%2e/etc/passwd/pdf",
        "/api/split/documents/..%2fbundle/pdf",
    ):
        assert client.get(path).status_code in {400, 404}


def test_workbench_requires_csrf_and_real_same_origin(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docorigin001", pages=8)
    app = create_app(cfg)
    valid = browser_client(app)
    token = valid.headers["X-Aktenfux-CSRF"]

    assert preview_document(valid, "docorigin001")

    missing_token = TestClient(app, base_url=BASE_URL, headers={"Origin": BASE_URL})
    blocked = missing_token.post(
        "/api/split/preview",
        json={"document_id": "docorigin001", "boundaries": [4]},
    )
    assert blocked.status_code == 403

    for origin in ("null", "http://evil.example", "http://127.0.0.1:9000"):
        hostile = TestClient(
            app,
            base_url=BASE_URL,
            headers={"Origin": origin, "X-Aktenfux-CSRF": token},
        )
        response = hostile.post(
            "/api/split/preview",
            json={"document_id": "docorigin001", "boundaries": [4]},
        )
        assert response.status_code == 403

    preflight = TestClient(app, base_url=BASE_URL).options(
        "/api/split/execute",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-aktenfux-csrf",
        },
    )
    assert preflight.status_code in {403, 405}
    assert "access-control-allow-origin" not in preflight.headers


def test_workbench_rejects_hostile_host(tmp_path):
    cfg = make_config(tmp_path)
    cfg.split_path.mkdir(parents=True)
    app = create_app(cfg)
    hostile = TestClient(app, base_url="http://evil.example")

    assert hostile.get("/").status_code == 400


def test_request_body_limit_rejects_before_route_parsing(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "doclimit001", pages=8)
    client = browser_client(create_app(cfg))
    called = False

    def unexpected_plan(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("route parser should not run")

    monkeypatch.setattr(workbench_module, "plan_split", unexpected_plan)
    response = client.post(
        "/api/split/preview",
        content=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": str(MAX_REQUEST_BODY_BYTES + 1)},
    )

    assert response.status_code == 413
    assert called is False


def test_streamed_request_body_limit_rejects_before_route_parsing(
    tmp_path, monkeypatch
):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docstream001", pages=8)
    client = browser_client(create_app(cfg))
    called = False

    def unexpected_plan(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("route parser should not run")

    monkeypatch.setattr(workbench_module, "plan_split", unexpected_plan)
    response = client.post(
        "/api/split/preview",
        content=(b"x" * 65_536 for _ in range(3)),
        headers={"Content-Type": "application/json"},
    )

    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    assert called is False


@pytest.mark.parametrize("content_length", ["invalid", "-1"])
def test_request_body_limit_rejects_invalid_content_length(tmp_path, content_length):
    cfg = make_config(tmp_path)
    cfg.split_path.mkdir(parents=True)
    client = browser_client(create_app(cfg))

    response = client.post(
        "/api/split/preview",
        content=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": content_length},
    )

    assert response.status_code == 400
    assert "content-length" in response.json()["detail"].lower()


def test_workbench_requires_literal_true_confirmation(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docconfirm001", pages=8)
    client = browser_client(create_app(cfg))
    preview = preview_document(client, "docconfirm001")

    for invalid in (None, False, "true", 1):
        payload = execute_payload("docconfirm001", preview)
        if invalid is None:
            payload.pop("confirm")
        else:
            payload["confirm"] = invalid
        response = client.post("/api/split/execute", json=payload)
        assert response.status_code == 422


def test_workbench_executes_exact_latest_plan_once(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docexact001", pages=8)
    app = create_app(cfg)
    client = browser_client(app)
    first = preview_document(client, "docexact001", boundaries=(4,))
    replacement = preview_document(client, "docexact001", boundaries=(2,))

    stale = client.post(
        "/api/split/execute",
        json=execute_payload(
            "docexact001",
            replacement,
            plan=first["plan"],
        ),
    )
    assert stale.status_code == 409
    assert "exact" in stale.json()["detail"].lower()

    execution = client.post(
        "/api/split/execute",
        json=execute_payload("docexact001", replacement),
    )
    assert execution.status_code == 200, execution.text
    assert [item["filename"] for item in execution.json()["result"]["outputs"]] == [
        "bundle--part-01.pdf",
        "bundle--part-02.pdf",
    ]

    duplicate = client.post(
        "/api/split/execute",
        json=execute_payload("docexact001", replacement),
    )
    assert duplicate.status_code == 409
    assert "already completed" in duplicate.json()["detail"].lower()


def test_identical_fresh_preview_invalidates_the_previous_token(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docnonce001", pages=8)
    client = browser_client(create_app(cfg))
    first = preview_document(client, "docnonce001", boundaries=(4,))
    replacement = preview_document(client, "docnonce001", boundaries=(4,))

    assert replacement["plan"] == first["plan"]
    assert replacement["preview_token"] != first["preview_token"]
    stale = client.post(
        "/api/split/execute",
        json=execute_payload("docnonce001", first),
    )

    assert stale.status_code == 409
    assert "stale" in stale.json()["detail"].lower()


def test_source_change_invalidates_preview_and_cleans_inflight(tmp_path):
    cfg = make_config(tmp_path)
    pdf = write_split_doc(cfg.split_path, "bundle.pdf", "docchange001", pages=8)
    app = create_app(cfg)
    client = browser_client(app)
    preview = preview_document(client, "docchange001")
    pdf.write_bytes(pdf.read_bytes() + b"changed")

    response = client.post(
        "/api/split/execute",
        json=execute_payload("docchange001", preview),
    )

    assert response.status_code in {400, 409}
    assert not app.state.in_flight
    assert not app.state.in_flight_documents


def test_unexpected_failure_invalidates_preview_and_cleans_inflight(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docfailure001", pages=8)
    app = create_app(cfg)
    client = browser_client(app)
    preview = preview_document(client, "docfailure001")

    def fail_execute(plan, config):
        raise RuntimeError("synthetic unexpected failure")

    monkeypatch.setattr(workbench_module, "execute_split", fail_execute)
    failed = client.post(
        "/api/split/execute",
        json=execute_payload("docfailure001", preview),
    )

    assert failed.status_code == 500
    assert not app.state.in_flight
    assert not app.state.in_flight_documents
    retry = client.post(
        "/api/split/execute",
        json=execute_payload("docfailure001", preview),
    )
    assert retry.status_code == 409
    assert "fresh preview" in retry.json()["detail"].lower()


def test_expected_service_failure_invalidates_preview_and_cleans_inflight(
    tmp_path, monkeypatch
):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docservice001", pages=8)
    app = create_app(cfg)
    client = browser_client(app)
    preview = preview_document(client, "docservice001")

    def fail_execute(plan, config):
        raise workbench_module.SplitError("synthetic expected failure")

    monkeypatch.setattr(workbench_module, "execute_split", fail_execute)
    failed = client.post(
        "/api/split/execute",
        json=execute_payload("docservice001", preview),
    )

    assert failed.status_code == 400
    assert not app.state.in_flight
    assert not app.state.in_flight_documents
    assert app.state.preview_store["docservice001"]["failed"] is True


def test_cancellation_invalidates_preview_and_cleans_inflight(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "doccancel001", pages=8)
    app = create_app(cfg)
    client = browser_client(app, raise_server_exceptions=False)
    preview = preview_document(client, "doccancel001")

    def cancel_execute(plan, config):
        raise asyncio.CancelledError

    monkeypatch.setattr(workbench_module, "execute_split", cancel_execute)
    response = client.post(
        "/api/split/execute",
        json=execute_payload("doccancel001", preview),
    )

    assert response.status_code == 500
    assert not app.state.in_flight
    assert not app.state.in_flight_documents
    assert app.state.preview_store["doccancel001"]["failed"] is True


def test_preview_is_blocked_while_document_execution_is_inflight(tmp_path):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docbusy001", pages=8)
    app = create_app(cfg)
    client = browser_client(app)
    app.state.in_flight_documents.add("docbusy001")

    response = client.post(
        "/api/split/preview",
        json={"document_id": "docbusy001", "boundaries": [4]},
    )

    assert response.status_code == 409


def test_duplicate_document_ids_fail_closed(tmp_path):
    cfg = make_config(tmp_path)
    first = write_split_doc(cfg.split_path, "a.pdf", "docduplicate001", pages=2)
    second = write_split_doc(cfg.split_path, "b.pdf", "docduplicate001", pages=2)
    assert first != second
    client = browser_client(create_app(cfg))

    response = client.get("/api/split/documents")

    assert response.status_code == 500
    assert "duplicate document id" in response.json()["detail"].lower()


def test_workbench_validates_loopback_binding() -> None:
    assert _validate_loopback_host("127.0.0.1") == "127.0.0.1"
    assert _validate_loopback_host("localhost") == "localhost"
    assert _validate_loopback_host("::1") == "::1"
    for host in ("0.0.0.0", "127.0.0.2", "example.com", ""):
        with pytest.raises(ValueError):
            _validate_loopback_host(host)
