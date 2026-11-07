"""Security and failure tests for the local split workbench."""
from __future__ import annotations

import asyncio
import threading
from io import BytesIO
import json
import re
from typing import Any
import pytest
import httpx
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

import aktenfux.workbench as workbench_module
from aktenfux.workbench import (
    MAX_REQUEST_BODY_BYTES,
    _validate_loopback_host,
    create_app,
)
from aktenfux.storage import sha256_file
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
    assert 'id="pdf-frame"' in response.text
    assert 'tabindex="-1"' in response.text
    assert "The embedded viewer receives only that page" in response.text
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    javascript = client.get("/workbench.js")
    assert javascript.status_code == 200
    assert javascript.headers["content-type"].startswith("application/javascript")
    assert "innerHTML" not in javascript.text


def test_workbench_lists_only_valid_split_documents_and_serves_pdf(tmp_path):
    cfg = make_config(tmp_path)
    pdf = write_split_doc(cfg.split_path, "bundle.pdf", "docbench000001", pages=6)
    original_pdf_bytes = pdf.read_bytes()
    original_sidecar_bytes = pdf.with_suffix(".json").read_bytes()
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
    assert original_pdf_bytes == pdf_response.content

    page_response = client.get(
        "/api/split/documents/docbench000001/pdf", params={"page": 4}
    )
    assert page_response.status_code == 200, page_response.text
    assert page_response.headers["content-type"].startswith("application/pdf")
    assert page_response.headers["x-aktenfux-document-page"] == "4"
    assert len(PdfReader(BytesIO(page_response.content)).pages) == 1
    assert page_response.content != original_pdf_bytes
    assert pdf.read_bytes() == original_pdf_bytes
    assert pdf.with_suffix(".json").read_bytes() == original_sidecar_bytes


@pytest.mark.parametrize("page", [0, 7, 5001])
def test_workbench_rejects_preview_page_outside_document(tmp_path, page):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docpagebounds001", pages=6)
    client = browser_client(create_app(cfg))

    response = client.get(
        "/api/split/documents/docpagebounds001/pdf", params={"page": page}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Requested PDF page is out of range."


def test_workbench_preview_contains_the_requested_source_page(tmp_path):
    cfg = make_config(tmp_path)
    pdf = write_split_doc(cfg.split_path, "sized.pdf", "docpageexact001", pages=3)
    writer = PdfWriter()
    for width in (100, 200, 300):
        writer.add_blank_page(width=width, height=400)
    with pdf.open("wb") as stream:
        writer.write(stream)
    sidecar = pdf.with_suffix(".json")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["sha256"] = sha256_file(pdf)
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    client = browser_client(create_app(cfg))

    response = client.get(
        "/api/split/documents/docpageexact001/pdf", params={"page": 2}
    )

    assert response.status_code == 200, response.text
    preview_page = PdfReader(BytesIO(response.content)).pages[0]
    assert float(preview_page.mediabox.width) == 200


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


def test_workbench_suggest_endpoint_returns_bounded_payload(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    pdf = write_split_doc(cfg.split_path, "bundle.pdf", "docsuggestapi0001", pages=6)
    source_sha256 = sha256_file(pdf)
    app = create_app(cfg)
    client = browser_client(app)

    monkeypatch.setattr(
        workbench_module,
        "suggest_split_boundaries",
        lambda document_id, config: {
            "document_id": "docsuggestapi0001",
            "source_sha256": source_sha256,
            "source_page_count": 6,
            "suggestion_provider": "ollama:qwen3:8b",
            "candidates": [
                {
                    "before_page": 4,
                    "confidence": 0.88,
                    "rationale": "A new sender block appears on page 4.",
                    "evidence": [{"page": 4, "cue": "From: Example Corp"}],
                }
            ],
            "warnings": [],
        },
    )

    response = client.post("/api/split/suggest", json={"document_id": "docsuggestapi0001"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["suggestion"]["document_id"] == "docsuggestapi0001"
    assert payload["suggestion"]["candidates"][0]["before_page"] == 4
    assert "path" not in response.text.lower()


def test_workbench_suggest_clears_inflight_on_failure(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docsuggestapi0002", pages=6)
    app = create_app(cfg)
    client = browser_client(app)

    def fail_suggest(document_id, config):
        raise workbench_module.SplitError("synthetic suggestion failure")

    monkeypatch.setattr(workbench_module, "suggest_split_boundaries", fail_suggest)
    response = client.post("/api/split/suggest", json={"document_id": "docsuggestapi0002"})

    assert response.status_code == 400
    assert "docsuggestapi0002" not in app.state.in_flight_suggestions


def test_workbench_suggest_rejects_duplicate_concurrent_request(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    pdf = write_split_doc(cfg.split_path, "bundle.pdf", "docsuggestapi0004", pages=6)
    source_sha256 = sha256_file(pdf)
    app = create_app(cfg)
    first_client = browser_client(app)
    second_client = browser_client(app)
    entered = threading.Event()
    release = threading.Event()
    call_count = {"value": 0}
    count_lock = threading.Lock()

    def slow_suggest(document_id, config):
        with count_lock:
            call_count["value"] += 1
        entered.set()
        release.wait(timeout=2)
        return {
            "document_id": document_id,
            "source_sha256": source_sha256,
            "source_page_count": 6,
            "suggestion_provider": "ollama:qwen3:8b",
            "candidates": [],
            "warnings": [],
        }

    monkeypatch.setattr(workbench_module, "suggest_split_boundaries", slow_suggest)
    first_response: dict[str, int] = {}

    def run_first_request():
        response = first_client.post("/api/split/suggest", json={"document_id": "docsuggestapi0004"})
        first_response["status_code"] = response.status_code

    worker = threading.Thread(target=run_first_request)
    worker.start()
    assert entered.wait(timeout=1)
    second = second_client.post("/api/split/suggest", json={"document_id": "docsuggestapi0004"})
    release.set()
    worker.join(timeout=2)

    assert second.status_code == 409
    assert first_response["status_code"] == 200
    assert call_count["value"] == 1


def test_workbench_suggest_timeout_keeps_reservation_until_worker_finishes(
    tmp_path, monkeypatch
):
    cfg = make_config(tmp_path)
    pdf = write_split_doc(cfg.split_path, "bundle.pdf", "docsuggestapi0005", pages=6)
    source_sha256 = sha256_file(pdf)
    app = create_app(cfg)
    monkeypatch.setattr(workbench_module, "SUGGEST_ROUTE_TIMEOUT_SECONDS", 0.05)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    counters = {"calls": 0, "active": 0, "max_active": 0}
    counter_lock = threading.Lock()

    def slow_suggest(document_id: str, config: Any) -> dict[str, Any]:
        with counter_lock:
            counters["calls"] += 1
            counters["active"] += 1
            counters["max_active"] = max(counters["max_active"], counters["active"])
        entered.set()
        try:
            release.wait(timeout=2)
            return {
                "document_id": document_id,
                "source_sha256": source_sha256,
                "source_page_count": 6,
                "suggestion_provider": "ollama:qwen3:8b",
                "candidates": [],
                "warnings": [],
            }
        finally:
            with counter_lock:
                counters["active"] -= 1
            finished.set()

    monkeypatch.setattr(workbench_module, "suggest_split_boundaries", slow_suggest)

    async def run_sequence() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            root = await client.get("/")
            token_match = TOKEN_PATTERN.search(root.text)
            assert token_match is not None
            token = token_match.group(1)
            headers = {"Origin": BASE_URL, "X-Aktenfux-CSRF": token}

            first = await client.post(
                "/api/split/suggest",
                json={"document_id": "docsuggestapi0005"},
                headers=headers,
            )
            assert first.status_code == 504
            assert entered.is_set()
            assert "docsuggestapi0005" in app.state.in_flight_suggestions
            with counter_lock:
                assert counters["calls"] == 1
                assert counters["active"] == 1

            second = await client.post(
                "/api/split/suggest",
                json={"document_id": "docsuggestapi0005"},
                headers=headers,
            )
            assert second.status_code == 409
            with counter_lock:
                assert counters["calls"] == 1
                assert counters["max_active"] == 1

            release.set()
            assert await asyncio.to_thread(finished.wait, 1)
            for _ in range(20):
                if "docsuggestapi0005" not in app.state.in_flight_suggestions:
                    break
                await asyncio.sleep(0.01)
            assert "docsuggestapi0005" not in app.state.in_flight_suggestions

            third = await client.post(
                "/api/split/suggest",
                json={"document_id": "docsuggestapi0005"},
                headers=headers,
            )
            assert third.status_code == 200
            with counter_lock:
                assert counters["calls"] == 2

    asyncio.run(run_sequence())


def test_workbench_suggest_cancelled_request_keeps_reservation_until_worker_finishes(
    tmp_path, monkeypatch
):
    cfg = make_config(tmp_path)
    pdf = write_split_doc(cfg.split_path, "bundle.pdf", "docsuggestapi0006", pages=6)
    source_sha256 = sha256_file(pdf)
    app = create_app(cfg)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    call_count = {"value": 0}
    count_lock = threading.Lock()

    def slow_suggest(document_id: str, config: Any) -> dict[str, Any]:
        with count_lock:
            call_count["value"] += 1
        entered.set()
        try:
            release.wait(timeout=2)
            return {
                "document_id": document_id,
                "source_sha256": source_sha256,
                "source_page_count": 6,
                "suggestion_provider": "ollama:qwen3:8b",
                "candidates": [],
                "warnings": [],
            }
        finally:
            finished.set()

    monkeypatch.setattr(workbench_module, "suggest_split_boundaries", slow_suggest)

    async def run_sequence() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            root = await client.get("/")
            token_match = TOKEN_PATTERN.search(root.text)
            assert token_match is not None
            token = token_match.group(1)
            headers = {"Origin": BASE_URL, "X-Aktenfux-CSRF": token}

            request_task = asyncio.create_task(
                client.post(
                    "/api/split/suggest",
                    json={"document_id": "docsuggestapi0006"},
                    headers=headers,
                )
            )
            assert await asyncio.to_thread(entered.wait, 1)
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task

            assert "docsuggestapi0006" in app.state.in_flight_suggestions
            with count_lock:
                assert call_count["value"] == 1

            second = await client.post(
                "/api/split/suggest",
                json={"document_id": "docsuggestapi0006"},
                headers=headers,
            )
            assert second.status_code == 409
            with count_lock:
                assert call_count["value"] == 1

            release.set()
            assert await asyncio.to_thread(finished.wait, 1)
            for _ in range(20):
                if "docsuggestapi0006" not in app.state.in_flight_suggestions:
                    break
                await asyncio.sleep(0.01)
            assert "docsuggestapi0006" not in app.state.in_flight_suggestions

            third = await client.post(
                "/api/split/suggest",
                json={"document_id": "docsuggestapi0006"},
                headers=headers,
            )
            assert third.status_code == 200
            with count_lock:
                assert call_count["value"] == 2

    asyncio.run(run_sequence())


def test_workbench_rejects_stale_suggestion_source_identity(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    write_split_doc(cfg.split_path, "bundle.pdf", "docsuggestapi0007", pages=6)
    app = create_app(cfg)
    client = browser_client(app)

    monkeypatch.setattr(
        workbench_module,
        "suggest_split_boundaries",
        lambda document_id, config: {
            "document_id": document_id,
            "source_sha256": "b" * 64,
            "source_page_count": 6,
            "suggestion_provider": "ollama:qwen3:8b",
            "candidates": [],
            "warnings": [],
        },
    )

    response = client.post("/api/split/suggest", json={"document_id": "docsuggestapi0007"})

    assert response.status_code == 409


def test_workbench_rejects_same_id_source_replacement_before_suggestion_response(
    tmp_path, monkeypatch
):
    cfg = make_config(tmp_path)
    pdf = write_split_doc(cfg.split_path, "bundle.pdf", "docsuggestapi0008", pages=6)
    app = create_app(cfg)
    client = browser_client(app)

    def stale_after_replacement(document_id, config):
        old_hash = sha256_file(pdf)
        pdf.write_bytes(pdf.read_bytes() + b"changed")
        return {
            "document_id": document_id,
            "source_sha256": old_hash,
            "source_page_count": 6,
            "suggestion_provider": "ollama:qwen3:8b",
            "candidates": [],
            "warnings": [],
        }

    monkeypatch.setattr(workbench_module, "suggest_split_boundaries", stale_after_replacement)
    response = client.post("/api/split/suggest", json={"document_id": "docsuggestapi0008"})

    assert response.status_code == 409
