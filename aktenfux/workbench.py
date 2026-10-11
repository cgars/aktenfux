"""Local split workbench for previewing and executing _Split PDF splits."""
from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse
except ModuleNotFoundError as exc:  # pragma: no cover - permissive import for runtime setup
    raise ModuleNotFoundError(
        "The split workbench requires the package dependencies from pyproject.toml. "
        "Install with 'pip install -e .'."
    ) from exc
from pydantic import BaseModel, Field

from aktenfux.config import AktenfuxConfig, load_config
from aktenfux.schema import SplitPlan, SplitResult
from aktenfux.split import (
    SplitError,
    get_split_document_snapshot,
    list_split_documents,
    plan_split,
    execute_split,
)

_INDEX_HTML = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
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
    ul { margin: 0; padding-left: 1.1rem; }
    li { cursor: pointer; padding: 0.2rem 0; }
    li.selected { color: #93c5fd; font-weight: 600; }
    .viewer-frame { flex: 1; min-height: 420px; border: 1px solid #475569; border-radius: 12px; background: #020617; display: flex; align-items: center; justify-content: center; overflow: hidden; position: relative; }
    .viewer-frame iframe { width: 100%; height: 100%; border: none; background: white; transform-origin: center center; }
    .boundary-marker { position: absolute; top: 0; bottom: 0; width: 3px; background: #f59e0b; box-shadow: 0 0 0 1px rgba(245,158,11,0.8); left: 50%; transform: translateX(-50%); opacity: 0.9; pointer-events: none; }
    .controls { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
    .range-list { margin-top: 0.5rem; }
    .status { min-height: 1.25rem; font-size: 0.95rem; }
    .danger { color: #fca5a5; }
    .ok { color: #86efac; }
    .confirmer { display: flex; align-items: center; gap: 0.6rem; }
  </style>
</head>
<body>
  <div class=\"layout\">
    <aside class=\"sidebar\">
      <h2>Split queue</h2>
      <div id=\"document-list\"></div>
    </aside>
    <main class=\"viewer\">
      <div class=\"panel\">
        <div class=\"controls\">
          <button id=\"prev-page\" class=\"secondary\" type=\"button\">Prev</button>
          <button id=\"next-page\" class=\"secondary\" type=\"button\">Next</button>
          <button id=\"zoom-out\" class=\"secondary\" type=\"button\">-</button>
          <button id=\"zoom-in\" class=\"secondary\" type=\"button\">+</button>
          <button id=\"rotate\" class=\"secondary\" type=\"button\">Rotate</button>
          <button id=\"toggle-boundary\" class=\"warning\" type=\"button\">Toggle boundary</button>
          <span id=\"page-info\">No document selected</span>
        </div>
      </div>

      <div class=\"viewer-frame\" id=\"viewer-frame\">
        <div id=\"boundary-marker\" class=\"boundary-marker\" aria-hidden=\"true\" style=\"display: none;\"></div>
        <iframe id=\"pdf-frame\" title=\"PDF viewer\" src=\"about:blank\"></iframe>
      </div>

      <div class=\"panel\">
        <div class=\"status\" id=\"message\"></div>
        <h3>Result preview</h3>
        <div id=\"preview-list\" class=\"range-list\">Select a document to preview.</div>
        <div class=\"confirmer\">
          <input id=\"confirm-check\" type=\"checkbox\" />
          <label for=\"confirm-check\">I confirm this exact plan.</label>
        </div>
        <div class=\"controls\" style=\"margin-top: 0.8rem;\">
          <button id=\"execute\" type=\"button\" disabled>Execute split</button>
        </div>
      </div>
    </main>
  </div>

  <script>
    const state = {
      documents: [],
      currentDocumentId: null,
      currentPage: 1,
      pageCount: 1,
      boundaries: [],
      previewToken: null,
      pendingPreview: false,
      pendingExecute: false,
      zoom: 1,
      rotation: 0,
      confirmChecked: false,
      preview: null,
    };

    const docList = document.getElementById('document-list');
    const previewList = document.getElementById('preview-list');
    const pdfFrame = document.getElementById('pdf-frame');
    const pageInfo = document.getElementById('page-info');
    const executeButton = document.getElementById('execute');
    const confirmCheck = document.getElementById('confirm-check');
    const messageEl = document.getElementById('message');
    const boundaryMarker = document.getElementById('boundary-marker');

    function setMessage(text, kind = 'info') {
      messageEl.textContent = text || '';
      messageEl.className = 'status';
      if (kind === 'error') messageEl.classList.add('danger');
      if (kind === 'ok') messageEl.classList.add('ok');
    }

    function resetConfirmationState() {
      state.confirmChecked = false;
      confirmCheck.checked = false;
      updateViewerUi();
    }

    function updateBoundaryMarker() {
      const markerVisible = !!state.currentDocumentId && state.boundaries.includes(state.currentPage);
      boundaryMarker.style.display = markerVisible ? 'block' : 'none';
      if (markerVisible) {
        const doc = state.documents.find((item) => item.id === state.currentDocumentId);
        const totalPages = doc ? doc.page_count : 1;
        const percent = (state.currentPage - 0.5) / totalPages * 100;
        boundaryMarker.style.left = `${Math.max(8, Math.min(92, percent))}%`;
      }
    }

    function updateViewTransform() {
      pdfFrame.style.transform = `scale(${state.zoom}) rotate(${state.rotation}deg)`;
    }

    function formatRange(part) {
      return `${part.start_page}-${part.end_page}`;
    }

    function renderPreviewParts(plan) {
      if (!plan || !Array.isArray(plan.parts)) {
        previewList.textContent = 'No preview available.';
        return;
      }
      previewList.textContent = '';
      plan.parts.forEach((part) => {
        const row = document.createElement('div');
        row.textContent = `pages ${formatRange(part)} → ${part.destination_filename}`;
        previewList.appendChild(row);
      });
    }

    async function loadDocuments() {
      const response = await fetch('/api/split/documents');
      if (!response.ok) {
        throw new Error('Could not load documents.');
      }
      const docs = await response.json();
      state.documents = docs;
      docList.innerHTML = '';
      if (!docs.length) {
        docList.innerHTML = '<p>No documents are currently eligible for splitting.</p>';
        state.currentDocumentId = null;
        state.preview = null;
        state.previewToken = null;
        pdfFrame.src = 'about:blank';
        resetConfirmationState();
        pageInfo.textContent = 'No document selected';
        previewList.textContent = 'Select a document to preview.';
        boundaryMarker.style.display = 'none';
        return;
      }
      docs.forEach((doc) => {
        const li = document.createElement('li');
        li.textContent = `${doc.label} (${doc.page_count} pages)`;
        li.className = doc.id === state.currentDocumentId ? 'selected' : '';
        li.onclick = () => selectDocument(doc.id);
        docList.appendChild(li);
      });
      if (state.currentDocumentId && !docs.some((item) => item.id === state.currentDocumentId)) {
        state.currentDocumentId = docs[0].id;
      }
      if (!state.currentDocumentId && docs.length) {
        state.currentDocumentId = docs[0].id;
      }
      if (state.currentDocumentId) {
        selectDocument(state.currentDocumentId, false);
      }
    }

    async function selectDocument(documentId, forceRefresh = true) {
      const doc = state.documents.find((item) => item.id === documentId);
      if (!doc) return;
      state.currentDocumentId = documentId;
      state.currentPage = Math.min(Math.max(1, state.currentPage), doc.page_count);
      state.boundaries = [];
      state.preview = null;
      state.previewToken = null;
      state.confirmChecked = false;
      confirmCheck.checked = false;
      if (forceRefresh) {
        await preview();
      }
      pdfFrame.src = `/api/split/documents/${documentId}/pdf#page=${state.currentPage}`;
      pageInfo.textContent = `Page ${state.currentPage}/${doc.page_count}`;
      refreshDocumentList();
      updateBoundaryMarker();
      updateViewerUi();
    }

    function refreshDocumentList() {
      [...docList.children].forEach((node, idx) => {
        const doc = state.documents[idx];
        node.classList.toggle('selected', doc && doc.id === state.currentDocumentId);
      });
    }

    function updateViewerUi() {
      const doc = state.documents.find((item) => item.id === state.currentDocumentId);
      const enabled = !!doc;
      const docPages = doc ? doc.page_count : 1;
      pageInfo.textContent = enabled ? `Page ${state.currentPage}/${docPages}` : 'No document selected';
      executeButton.disabled = !state.preview || !state.confirmChecked || state.pendingExecute;
      confirmCheck.disabled = !state.preview || state.pendingExecute;
      document.getElementById('toggle-boundary').disabled = !enabled || state.pendingPreview || state.pendingExecute;
      document.getElementById('prev-page').disabled = !enabled;
      document.getElementById('next-page').disabled = !enabled;
      updateBoundaryMarker();
    }

    async function preview() {
      const doc = state.documents.find((item) => item.id === state.currentDocumentId);
      if (!doc) return;
      state.pendingPreview = true;
      resetConfirmationState();
      const payload = { document_id: doc.id, boundaries: [...state.boundaries].sort((a, b) => a - b) };
      try {
        const response = await fetch('/api/split/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const body = await response.json();
        if (!response.ok) {
          throw new Error(body.detail || 'Invalid split preview.');
        }
        state.preview = body.plan;
        state.previewToken = body.preview_token;
        renderPreviewParts(body.plan);
        setMessage(`Preview ready for ${body.plan.document_id}.`, 'ok');
      } catch (error) {
        previewList.textContent = (error && error.message) || 'Preview failed.';
        setMessage((error && error.message) || 'Preview failed.', 'error');
        state.preview = null;
        state.previewToken = null;
      } finally {
        state.pendingPreview = false;
        updateViewerUi();
      }
    }

    async function executePlan() {
      const doc = state.documents.find((item) => item.id === state.currentDocumentId);
      if (!doc || !state.preview || !state.previewToken || state.pendingExecute) return;
      state.pendingExecute = true;
      updateViewerUi();
      try {
        const response = await fetch('/api/split/execute', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            document_id: doc.id,
            plan: state.preview,
            preview_token: state.previewToken,
          }),
        });
        const body = await response.json();
        if (!response.ok) {
          throw new Error(body.detail || 'Split execution failed.');
        }
        setMessage(`Split complete: ${body.result.outputs.length} parts created.`, 'ok');
        state.preview = null;
        state.previewToken = null;
        confirmCheck.checked = false;
        state.confirmChecked = false;
        await loadDocuments();
      } catch (error) {
        setMessage((error && error.message) || 'Split execution failed.', 'error');
      } finally {
        state.pendingExecute = false;
        updateViewerUi();
      }
    }

    function toggleBoundary() {
      if (!state.currentDocumentId || state.pendingPreview || state.pendingExecute) return;
      const page = state.currentPage;
      if (page <= 1) return;
      const exists = state.boundaries.includes(page);
      if (exists) {
        state.boundaries = state.boundaries.filter((value) => value !== page);
      } else {
        state.boundaries = [...state.boundaries, page].sort((a, b) => a - b);
      }
      preview();
    }

    document.getElementById('zoom-in').onclick = () => { state.zoom = Math.min(2.5, Number((state.zoom + 0.25).toFixed(2))); updateViewTransform(); };
    document.getElementById('zoom-out').onclick = () => { state.zoom = Math.max(0.5, Number((state.zoom - 0.25).toFixed(2))); updateViewTransform(); };
    document.getElementById('rotate').onclick = () => { state.rotation = (state.rotation + 90) % 360; updateViewTransform(); };
    document.getElementById('prev-page').onclick = () => { const doc = state.documents.find((item) => item.id === state.currentDocumentId); if (!doc) return; if (state.currentPage > 1) { state.currentPage -= 1; pdfFrame.src = `/api/split/documents/${doc.id}/pdf#page=${state.currentPage}`; pageInfo.textContent = `Page ${state.currentPage}/${doc.page_count}`; updateBoundaryMarker(); } };
    document.getElementById('next-page').onclick = () => { const doc = state.documents.find((item) => item.id === state.currentDocumentId); if (!doc) return; if (state.currentPage < doc.page_count) { state.currentPage += 1; pdfFrame.src = `/api/split/documents/${doc.id}/pdf#page=${state.currentPage}`; pageInfo.textContent = `Page ${state.currentPage}/${doc.page_count}`; updateBoundaryMarker(); } };
    document.getElementById('toggle-boundary').onclick = toggleBoundary;
    confirmCheck.onchange = () => {
      state.confirmChecked = confirmCheck.checked;
      updateViewerUi();
    };
    executeButton.onclick = executePlan;

    document.addEventListener('keydown', (event) => {
      if (event.key.toLowerCase() === 't') {
        toggleBoundary();
      }
    });

    updateViewTransform();
    loadDocuments().catch((error) => {
      setMessage(error.message || 'Could not load documents.', 'error');
    });
  </script>
</body>
</html>
"""


class PreviewRequest(BaseModel):
    document_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    boundaries: list[int] = Field(..., max_length=64)


class ExecuteRequest(BaseModel):
    document_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    preview_token: str = Field(..., min_length=8, max_length=128, pattern=r"^[A-Fa-f0-9]+$")
    plan: dict[str, Any]


class SplitDocumentDTO(BaseModel):
    id: str
    label: str
    name: str
    page_count: int
    pdf_url: str


def _read_sidecar_from_pdf(pdf_path: Path):
    from aktenfux.storage import read_sidecar

    sidecar = read_sidecar(pdf_path)
    if sidecar is None:
        raise SplitError(f"Missing sidecar for {pdf_path.name}.")
    return sidecar


def _preview_token(plan: SplitPlan) -> str:
    payload = json.dumps(plan.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_and_normalize_boundaries(value: list[int]) -> list[int]:
    normalized = []
    seen: set[int] = set()
    for boundary in value:
        if boundary < 2 or boundary > 5000:
            raise ValueError("Boundary out of range.")
        if boundary in seen:
            raise ValueError("Duplicate boundaries are not allowed.")
        seen.add(boundary)
        normalized.append(boundary)
    return sorted(normalized)


def create_app(config: AktenfuxConfig | None = None) -> FastAPI:
    cfg = config or load_config()
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        app.state.execute_lock = asyncio.Lock()
        yield

    app = FastAPI(title="Aktenfux Split Workbench", version="0.1.0", lifespan=lifespan)
    app.state.config = cfg
    app.state.preview_store: dict[str, dict[str, Any]] = {}
    app.state.execute_lock: asyncio.Lock | None = None
    app.state.in_flight: set[str] = set()

    async def _ensure_lock() -> asyncio.Lock:
        if app.state.execute_lock is None:
            app.state.execute_lock = asyncio.Lock()
        return app.state.execute_lock

    @app.get("/", response_class=HTMLResponse)
    async def root() -> HTMLResponse:
        return HTMLResponse(_INDEX_HTML)

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
                id=document_id,
                label=label,
                name=label,
                page_count=page_count,
                pdf_url=f"/api/split/documents/{document_id}/pdf",
            )
            for document_id, label, page_count in entries
        ]

    @app.get("/api/split/documents/{document_id}/pdf")
    async def serve_document_pdf(document_id: str) -> FileResponse:
        if not _is_valid_document_id(document_id):
            raise HTTPException(status_code=400, detail="Unexpected document identifier.")
        try:
            snapshot = get_split_document_snapshot(cfg, document_id)
        except (SplitError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            resolved = snapshot.pdf_path.resolve(strict=True)
            split_root = cfg.split_path.resolve(strict=True)
            if resolved.parent != split_root:
                raise ValueError("PDF path must remain in _Split.")
            if resolved.is_symlink():
                raise ValueError("PDF path must not be a symbolic link.")
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=403, detail="PDF path is outside the expected split root.") from exc
        return FileResponse(path=resolved, media_type="application/pdf")

    @app.post("/api/split/preview")
    async def preview(request: PreviewRequest) -> dict[str, Any]:
        try:
            safe_boundaries = _validate_and_normalize_boundaries(request.boundaries)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            plan = plan_split(request.document_id, safe_boundaries, cfg)
        except (SplitError, ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        token = _preview_token(plan)
        app.state.preview_store[request.document_id] = {
            "token": token,
            "plan": plan.model_dump(mode="json"),
            "completed": False,
        }
        return {"document_id": request.document_id, "plan": plan.model_dump(mode="json"), "preview_token": token}

    @app.post("/api/split/execute")
    async def execute(request: ExecuteRequest) -> dict[str, Any]:
        if not request.preview_token or len(request.preview_token) < 8:
            raise HTTPException(status_code=400, detail="Preview token is invalid.")
        if not isinstance(request.plan, dict):
            raise HTTPException(status_code=400, detail="Plan payload is invalid.")
        lock = await _ensure_lock()
        async with lock:
            current = app.state.preview_store.get(request.document_id)
            if current is None:
                raise HTTPException(status_code=409, detail="No preview has been generated for this document.")
            if current.get("failed") is True:
                raise HTTPException(status_code=409, detail="This preview is no longer valid; create a fresh preview before retrying.")
            if current.get("completed") is True:
                raise HTTPException(status_code=409, detail="Split execution has already completed for this preview.")
            if current["token"] != request.preview_token:
                raise HTTPException(status_code=409, detail="Stale or mismatched preview token; preview again before executing.")

            try:
                plan = SplitPlan.model_validate(request.plan)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail="Plan payload is invalid.") from exc

            expected_operation = current["plan"].get("operation_id")
            if plan.operation_id != expected_operation:
                raise HTTPException(status_code=409, detail="Execution requires the exact most recently previewed plan.")

            try:
                snapshot = get_split_document_snapshot(cfg, request.document_id)
            except SplitError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            for prior in snapshot.sidecar.split_operations:
                if prior.operation_id == plan.operation_id:
                    if prior.state == "completed":
                        raise HTTPException(status_code=409, detail="Split execution has already completed for this preview.")
                    if prior.state == "in_progress":
                        raise HTTPException(status_code=409, detail="Split execution is already in progress.")

            if plan.operation_id in app.state.in_flight:
                raise HTTPException(status_code=409, detail="Split execution is already in progress.")
            app.state.in_flight.add(plan.operation_id)

        try:
            result = execute_split(plan, cfg)
        except (SplitError, ValueError, FileNotFoundError, FileExistsError) as exc:
            async with lock:
                app.state.preview_store[request.document_id] = {
                    "token": current["token"],
                    "plan": current["plan"],
                    "completed": False,
                    "failed": True,
                }
                app.state.in_flight.discard(plan.operation_id)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        async with lock:
            app.state.in_flight.discard(plan.operation_id)
            app.state.preview_store[request.document_id] = {
                "token": current["token"],
                "plan": current["plan"],
                "completed": True,
            }
        return {"result": result.model_dump(mode="json")}

    return app


def _is_valid_document_id(value: str) -> bool:
    return bool(value) and all(ch.isalnum() or ch in {"_", "-"} for ch in value)


def _validate_split_roots(config: AktenfuxConfig, *, include_inbox: bool) -> None:
    from aktenfux.split import _validate_split_roots as _validate_roots

    _validate_roots(config, include_inbox=include_inbox)


def _serve_from_module(host: str, port: int, config_path: Path | None = None) -> None:
    import uvicorn

    cfg = load_config(config_path)
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the Aktenfux split workbench.")
    parser.add_argument("--config", type=Path, default=None, help="Configuration file to use.")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback host to bind to.")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to.")
    args = parser.parse_args()
    _serve_from_module(args.host, args.port, args.config)
