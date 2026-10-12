"""Local, loopback-only split workbench."""
from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import secrets
from collections import deque
from contextlib import asynccontextmanager
from hmac import compare_digest
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.trustedhost import TrustedHostMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
except ModuleNotFoundError as exc:  # pragma: no cover - permissive import for runtime setup
    raise ModuleNotFoundError(
        "The split workbench requires the package dependencies from pyproject.toml. "
        "Install with 'pip install -e .'."
    ) from exc
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aktenfux.config import AktenfuxConfig, load_config
from aktenfux.schema import SplitPlan
from aktenfux.split import (
    SplitError,
    execute_split,
    get_split_document_snapshot,
    list_split_documents,
    plan_split,
)

MAX_REQUEST_BODY_BYTES = 128 * 1024
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_DOCUMENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_WORKBENCH_JS = Path(__file__).with_name("workbench.js").read_text(encoding="utf-8")

_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="aktenfux-csrf-token" content="__CSRF_TOKEN__" />
  <title>Aktenfux Split Workbench</title>
  <style>
    :root { color-scheme: light dark; }
    body { font-family: sans-serif; margin: 0; background: #111827; color: #e5e7eb; }
    .layout { display: grid; grid-template-columns: 320px 1fr; min-height: 100vh; }
    .sidebar { background: #0f172a; border-right: 1px solid #334155; padding: 1rem; overflow-y: auto; }
    .viewer { padding: 1rem; display: flex; flex-direction: column; gap: 0.75rem; }
    .panel { background: rgba(15, 23, 42, 0.75); border: 1px solid #334155; border-radius: 12px; padding: 0.8rem; }
    button, select, input { font: inherit; }
    button { background: #2563eb; color: white; border: none; border-radius: 8px; padding: 0.55rem 0.8rem; cursor: pointer; }
    button.secondary { background: #475569; }
    button.warning { background: #b45309; }
    button:disabled { opacity: 0.55; cursor: not-allowed; }
    .document-choice { display: block; width: 100%; margin-bottom: 0.4rem; text-align: left; background: #1e293b; }
    .document-choice.selected { color: #bfdbfe; outline: 2px solid #60a5fa; }
    .viewer-frame { flex: 1; min-height: 420px; border: 1px solid #475569; border-radius: 12px; background: #020617; display: flex; align-items: center; justify-content: center; overflow: hidden; position: relative; }
    .viewer-frame iframe { width: 100%; height: 100%; border: none; background: white; transform-origin: center center; }
    .boundary-marker { position: absolute; z-index: 2; top: 0.75rem; left: 50%; transform: translateX(-50%); padding: 0.45rem 0.75rem; border: 2px solid #fbbf24; border-radius: 999px; color: #111827; background: #fbbf24; font-weight: 700; pointer-events: none; }
    .controls { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
    .range-list { margin-top: 0.5rem; }
    .status { min-height: 1.25rem; font-size: 0.95rem; }
    .danger { color: #fca5a5; }
    .ok { color: #86efac; }
    .confirmer { display: flex; align-items: center; gap: 0.6rem; }
  </style>
  <script src="/workbench.js" defer></script>
</head>
<body>
  <div class="layout">
    <aside class="sidebar">
      <h2>Split queue</h2>
      <div id="document-list"></div>
    </aside>
    <main class="viewer">
      <div class="panel">
        <div class="controls">
          <button id="prev-page" class="secondary" type="button">Prev</button>
          <button id="next-page" class="secondary" type="button">Next</button>
          <button id="zoom-out" class="secondary" type="button">−</button>
          <button id="zoom-in" class="secondary" type="button">+</button>
          <button id="rotate" class="secondary" type="button">Rotate</button>
          <button id="toggle-boundary" class="warning" type="button">Toggle boundary</button>
          <button id="refresh-preview" class="secondary" type="button">Preview split</button>
          <span id="page-info">No document selected</span>
        </div>
      </div>
      <div class="viewer-frame" id="viewer-frame">
        <div id="boundary-marker" class="boundary-marker" role="status" hidden></div>
        <iframe id="pdf-frame" title="PDF viewer" src="about:blank"></iframe>
      </div>
      <div class="panel">
        <div class="status" id="message"></div>
        <h3>Result preview</h3>
        <div id="preview-list" class="range-list">Select a document to preview.</div>
        <div class="confirmer">
          <input id="confirm-check" type="checkbox" />
          <label for="confirm-check">I confirm this exact plan.</label>
        </div>
        <div class="controls" style="margin-top: 0.8rem;">
          <button id="execute" type="button" disabled>Execute split</button>
        </div>
      </div>
    </main>
  </div>
</body>
</html>
"""


async def _send_error(
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    status_code: int,
    detail: str,
) -> None:
    response = JSONResponse(status_code=status_code, content={"detail": detail})
    await response(scope, receive, send)


class RequestBodyLimitMiddleware:
    """Read at most ``max_bytes`` before a mutating body reaches the parser."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in _MUTATING_METHODS:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                await _send_error(
                    scope,
                    receive,
                    send,
                    status_code=400,
                    detail="Content-Length is invalid.",
                )
                return
            if declared_length < 0:
                await _send_error(
                    scope,
                    receive,
                    send,
                    status_code=400,
                    detail="Content-Length is invalid.",
                )
                return
            if declared_length > self.max_bytes:
                await _send_error(
                    scope,
                    receive,
                    send,
                    status_code=413,
                    detail="Request body is too large.",
                )
                return

        received = 0
        messages: deque[Message] = deque()
        while True:
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    await _send_error(
                        scope,
                        receive,
                        send,
                        status_code=413,
                        detail="Request body is too large.",
                    )
                    return
                messages.append(message)
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                messages.append(message)
                break

        async def replay_receive() -> Message:
            if messages:
                return messages.popleft()
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


def _same_origin(origin: str, host: str) -> bool:
    try:
        parsed_origin = urlsplit(origin)
        parsed_host = urlsplit(f"//{host}")
    except ValueError:
        return False
    if parsed_origin.scheme != "http" or parsed_origin.username or parsed_origin.password:
        return False
    if parsed_origin.path not in {"", "/"} or parsed_origin.query or parsed_origin.fragment:
        return False
    if not parsed_origin.hostname or not parsed_host.hostname:
        return False
    origin_port = parsed_origin.port or 80
    host_port = parsed_host.port or 80
    return (
        parsed_origin.hostname.lower() == parsed_host.hostname.lower()
        and origin_port == host_port
    )


class WorkbenchRequestSecurityMiddleware:
    """Require the page token and a matching browser origin for API writes."""

    def __init__(self, app: ASGIApp, csrf_token: str) -> None:
        self.app = app
        self.csrf_token = csrf_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") in _MUTATING_METHODS
            and str(scope.get("path", "")).startswith("/api/split")
        ):
            supplied_token = _header(scope, b"x-aktenfux-csrf")
            if not supplied_token or not compare_digest(supplied_token, self.csrf_token):
                await _send_error(
                    scope,
                    receive,
                    send,
                    status_code=403,
                    detail="A valid workbench security token is required.",
                )
                return
            origin = _header(scope, b"origin").strip()
            host = _header(scope, b"host").strip()
            if origin and (origin.lower() == "null" or not _same_origin(origin, host)):
                await _send_error(
                    scope,
                    receive,
                    send,
                    status_code=403,
                    detail="Cross-origin requests are not allowed.",
                )
                return
        await self.app(scope, receive, send)


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    document_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    boundaries: list[int] = Field(..., min_length=1, max_length=64)

    @field_validator("boundaries")
    @classmethod
    def validate_boundaries(cls, value: list[int]) -> list[int]:
        if len(value) > 64:
            raise ValueError("Too many boundaries for a split preview.")
        return value


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    document_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    preview_token: str = Field(..., min_length=64, max_length=64, pattern=r"^[a-f0-9]+$")
    plan: dict[str, Any] = Field(..., min_length=1, max_length=128)
    confirm: StrictBool

    @field_validator("confirm")
    @classmethod
    def require_confirmation(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Execution requires explicit confirmation.")
        return value

    @field_validator("plan")
    @classmethod
    def validate_plan_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, separators=(",", ":"), sort_keys=True)) > 100_000:
            raise ValueError("Plan payload is too large.")
        return value


class SplitDocumentDTO(BaseModel):
    id: str
    label: str
    name: str
    page_count: int
    pdf_url: str


def _new_preview_token() -> str:
    """Return a one-time token so even an identical new preview invalidates the old one."""
    return secrets.token_hex(32)


def _validate_and_normalize_boundaries(value: list[int]) -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()
    for boundary in value:
        if boundary < 2 or boundary > 5000:
            raise ValueError("Boundary out of range.")
        if boundary in seen:
            raise ValueError("Duplicate boundaries are not allowed.")
        seen.add(boundary)
        normalized.append(boundary)
    return sorted(normalized)


def _security_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'unsafe-inline'; "
            "frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


def create_app(config: AktenfuxConfig | None = None) -> FastAPI:
    cfg = config or load_config()
    csrf_token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        app.state.execute_lock = asyncio.Lock()
        yield

    app = FastAPI(title="Aktenfux Split Workbench", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "[::1]", "::1", "testserver"],
    )
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)
    app.add_middleware(WorkbenchRequestSecurityMiddleware, csrf_token=csrf_token)
    app.state.config = cfg
    app.state.csrf_token = csrf_token
    app.state.preview_store: dict[str, dict[str, Any]] = {}
    app.state.execute_lock: asyncio.Lock | None = None
    app.state.in_flight: set[str] = set()
    app.state.in_flight_documents: set[str] = set()

    async def ensure_lock() -> asyncio.Lock:
        if app.state.execute_lock is None:
            app.state.execute_lock = asyncio.Lock()
        return app.state.execute_lock

    def mark_preview_failed(document_id: str, token: str) -> None:
        current = app.state.preview_store.get(document_id)
        if current is not None and current.get("token") == token:
            current["completed"] = False
            current["failed"] = True

    @app.get("/", response_class=HTMLResponse)
    async def root() -> HTMLResponse:
        page = _INDEX_HTML.replace("__CSRF_TOKEN__", html.escape(csrf_token, quote=True))
        return HTMLResponse(page, headers=_security_headers())

    @app.get("/workbench.js")
    async def workbench_javascript() -> Response:
        return Response(
            _WORKBENCH_JS,
            media_type="application/javascript",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/split/documents", response_model=list[SplitDocumentDTO])
    async def list_documents() -> list[SplitDocumentDTO]:
        try:
            entries = list_split_documents(cfg)
        except SplitError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return [
            SplitDocumentDTO(
                id=entry.document_id,
                label=entry.display_name,
                name=entry.display_name,
                page_count=entry.page_count,
                pdf_url=f"/api/split/documents/{entry.document_id}/pdf",
            )
            for entry in entries
        ]

    @app.get("/api/split/documents/{document_id}/pdf")
    async def serve_document_pdf(document_id: str) -> FileResponse:
        decoded_document_id = unquote(document_id)
        if not _is_valid_document_id(decoded_document_id):
            raise HTTPException(status_code=400, detail="Unexpected document identifier.")
        try:
            snapshot = get_split_document_snapshot(cfg, decoded_document_id)
        except (SplitError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            resolved = snapshot.pdf_path.resolve(strict=True)
            split_root = cfg.split_path.resolve(strict=True)
            if resolved.parent != split_root or snapshot.pdf_path.is_symlink():
                raise ValueError("PDF path must remain in _Split.")
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=403,
                detail="PDF path is outside the expected split root.",
            ) from exc
        return FileResponse(path=resolved, media_type="application/pdf")

    @app.get("/api/split/documents/{document_id:path}")
    async def document_identifier_guard(document_id: str) -> dict[str, str]:
        decoded_document_id = unquote(document_id)
        if not _is_valid_document_id(decoded_document_id):
            raise HTTPException(status_code=400, detail="Unexpected document identifier.")
        raise HTTPException(status_code=404, detail="Document not found.")

    @app.post("/api/split/preview")
    async def preview(request: PreviewRequest) -> dict[str, Any]:
        try:
            safe_boundaries = _validate_and_normalize_boundaries(request.boundaries)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        lock = await ensure_lock()
        async with lock:
            if request.document_id in app.state.in_flight_documents:
                raise HTTPException(
                    status_code=409,
                    detail="Split execution is already in progress for this document.",
                )
        try:
            plan = plan_split(request.document_id, safe_boundaries, cfg)
        except (SplitError, ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        token = _new_preview_token()
        async with lock:
            if request.document_id in app.state.in_flight_documents:
                raise HTTPException(
                    status_code=409,
                    detail="Split execution is already in progress for this document.",
                )
            app.state.preview_store[request.document_id] = {
                "token": token,
                "plan": plan.model_dump(mode="json"),
                "completed": False,
                "failed": False,
            }
        return {
            "document_id": request.document_id,
            "plan": plan.model_dump(mode="json"),
            "preview_token": token,
        }

    @app.post("/api/split/execute")
    async def execute(request: ExecuteRequest) -> dict[str, Any]:
        lock = await ensure_lock()
        async with lock:
            current = app.state.preview_store.get(request.document_id)
            if current is None:
                raise HTTPException(
                    status_code=409,
                    detail="No preview has been generated for this document.",
                )
            if current.get("failed") is True:
                raise HTTPException(
                    status_code=409,
                    detail="This preview is no longer valid; create a fresh preview before retrying.",
                )
            if current.get("completed") is True:
                raise HTTPException(
                    status_code=409,
                    detail="Split execution has already completed for this preview.",
                )
            if current["token"] != request.preview_token:
                raise HTTPException(
                    status_code=409,
                    detail="Stale or mismatched preview token; preview again before executing.",
                )
            try:
                plan = SplitPlan.model_validate(request.plan)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail="Plan payload is invalid.") from exc
            if current["plan"] != request.plan:
                raise HTTPException(
                    status_code=409,
                    detail="Execution requires the exact most recently previewed plan.",
                )

            try:
                snapshot = get_split_document_snapshot(cfg, request.document_id)
            except SplitError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            for prior in snapshot.sidecar.split_operations:
                if prior.operation_id == plan.operation_id:
                    if prior.state == "completed":
                        raise HTTPException(
                            status_code=409,
                            detail="Split execution has already completed for this preview.",
                        )
                    if prior.state == "in_progress":
                        raise HTTPException(
                            status_code=409,
                            detail="Split execution is already in progress.",
                        )
            if plan.operation_id in app.state.in_flight:
                raise HTTPException(
                    status_code=409,
                    detail="Split execution is already in progress.",
                )
            app.state.in_flight.add(plan.operation_id)
            app.state.in_flight_documents.add(request.document_id)

        try:
            result = execute_split(plan, cfg)
        except (SplitError, ValueError, FileNotFoundError, FileExistsError) as exc:
            mark_preview_failed(request.document_id, request.preview_token)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except asyncio.CancelledError:
            mark_preview_failed(request.document_id, request.preview_token)
            raise
        except Exception as exc:  # noqa: BLE001
            mark_preview_failed(request.document_id, request.preview_token)
            raise HTTPException(
                status_code=500,
                detail="Split execution failed unexpectedly; create a fresh preview before retrying.",
            ) from exc
        else:
            current = app.state.preview_store.get(request.document_id)
            if current is not None and current.get("token") == request.preview_token:
                current["completed"] = True
                current["failed"] = False
            return {"result": result.model_dump(mode="json")}
        finally:
            # These non-awaiting mutations must run even while task cancellation is active.
            app.state.in_flight.discard(plan.operation_id)
            app.state.in_flight_documents.discard(request.document_id)

    return app


def _is_valid_document_id(value: str) -> bool:
    return bool(_DOCUMENT_ID_PATTERN.fullmatch(value))


def _validate_loopback_host(host: str) -> str:
    if not host:
        raise ValueError("Host must not be empty.")
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.version == 6 and parsed == ipaddress.ip_address("::1"):
            return host
        if parsed.version == 4 and parsed == ipaddress.ip_address("127.0.0.1"):
            return host
        raise ValueError("The split workbench must bind only to loopback addresses.")
    if host.lower() == "localhost":
        return host
    raise ValueError("The split workbench must bind only to loopback addresses.")


def _serve_from_module(host: str, port: int, config_path: Path | None = None) -> None:
    import uvicorn

    cfg = load_config(config_path)
    bind_host = _validate_loopback_host(host)
    uvicorn.run(create_app(cfg), host=bind_host, port=port, log_level="warning")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the Aktenfux split workbench.")
    parser.add_argument("--config", type=Path, default=None, help="Configuration file to use.")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback host to bind to.")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to.")
    args = parser.parse_args()
    _serve_from_module(args.host, args.port, args.config)
