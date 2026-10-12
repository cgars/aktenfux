(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof document !== "undefined") {
    const start = () => api.startWorkbench(document, root.fetch.bind(root));
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", start, { once: true });
    } else {
      start();
    }
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function clampPage(page, pageCount) {
    return Math.min(Math.max(1, page), Math.max(1, pageCount));
  }

  function movePage(currentPage, pageCount, delta) {
    return clampPage(currentPage + delta, pageCount);
  }

  function pdfUrl(documentId, page) {
    return `/api/split/documents/${encodeURIComponent(documentId)}/pdf#page=${page}`;
  }

  function viewerTransform(zoom, rotation) {
    return `scale(${zoom}) rotate(${rotation}deg)`;
  }

  function boundaryPresentation(boundaries, currentPage) {
    const visible = currentPage > 1 && boundaries.includes(currentPage);
    return {
      visible,
      text: visible ? `Split before page ${currentPage}` : "",
    };
  }

  function canExecute(state) {
    return Boolean(
      state.preview &&
      state.previewToken &&
      state.confirmChecked &&
      !state.pendingPreview &&
      !state.pendingExecute
    );
  }

  function buildExecutePayload(state) {
    if (!canExecute(state)) return null;
    return {
      document_id: state.currentDocumentId,
      plan: state.preview,
      preview_token: state.previewToken,
      confirm: true,
    };
  }

  function invalidatePlanState(state) {
    state.previewSequence += 1;
    state.preview = null;
    state.previewToken = null;
    state.confirmChecked = false;
    return state;
  }

  function setText(node, value) {
    node.textContent = String(value ?? "");
  }

  function renderPreviewParts(container, plan, createElement) {
    if (!plan || !Array.isArray(plan.parts)) {
      setText(container, "No preview available.");
      return;
    }
    container.replaceChildren();
    plan.parts.forEach((part) => {
      const row = createElement("div");
      setText(
        row,
        `pages ${part.start_page}-${part.end_page} → ${part.destination_filename}`
      );
      container.appendChild(row);
    });
  }

  function startWorkbench(doc, fetchImpl) {
    const state = {
      documents: [],
      currentDocumentId: null,
      currentPage: 1,
      boundaries: [],
      previewToken: null,
      previewSequence: 0,
      previewController: null,
      pendingPreview: false,
      pendingExecute: false,
      zoom: 1,
      rotation: 0,
      confirmChecked: false,
      preview: null,
    };

    const elements = {
      docList: doc.getElementById("document-list"),
      previewList: doc.getElementById("preview-list"),
      pdfFrame: doc.getElementById("pdf-frame"),
      pageInfo: doc.getElementById("page-info"),
      executeButton: doc.getElementById("execute"),
      confirmCheck: doc.getElementById("confirm-check"),
      message: doc.getElementById("message"),
      boundaryMarker: doc.getElementById("boundary-marker"),
      prevPage: doc.getElementById("prev-page"),
      nextPage: doc.getElementById("next-page"),
      zoomOut: doc.getElementById("zoom-out"),
      zoomIn: doc.getElementById("zoom-in"),
      rotate: doc.getElementById("rotate"),
      toggleBoundary: doc.getElementById("toggle-boundary"),
      refreshPreview: doc.getElementById("refresh-preview"),
    };
    const csrfToken = doc.querySelector('meta[name="aktenfux-csrf-token"]')?.content;
    if (!csrfToken) {
      throw new Error("The workbench security token is missing.");
    }

    function requestHeaders() {
      return {
        "Content-Type": "application/json",
        "X-Aktenfux-CSRF": csrfToken,
      };
    }

    function selectedDocument() {
      return state.documents.find((item) => item.id === state.currentDocumentId) || null;
    }

    function setMessage(text, kind = "info") {
      setText(elements.message, text || "");
      elements.message.className = "status";
      if (kind === "error") elements.message.classList.add("danger");
      if (kind === "ok") elements.message.classList.add("ok");
    }

    function clearConfirmation() {
      state.confirmChecked = false;
      elements.confirmCheck.checked = false;
    }

    function abortPreview() {
      if (state.previewController) {
        state.previewController.abort();
        state.previewController = null;
      }
    }

    function invalidatePreview() {
      abortPreview();
      invalidatePlanState(state);
      state.pendingPreview = false;
      clearConfirmation();
    }

    function updateBoundaryMarker() {
      const presentation = boundaryPresentation(state.boundaries, state.currentPage);
      elements.boundaryMarker.hidden = !presentation.visible;
      setText(elements.boundaryMarker, presentation.text);
    }

    function updateViewTransform() {
      elements.pdfFrame.style.transform = viewerTransform(state.zoom, state.rotation);
    }

    function updateViewerUi() {
      const selected = selectedDocument();
      const enabled = Boolean(selected);
      setText(
        elements.pageInfo,
        enabled
          ? `Page ${state.currentPage}/${selected.page_count}`
          : "No document selected"
      );
      elements.executeButton.disabled = !canExecute(state);
      elements.confirmCheck.disabled = !state.preview || state.pendingExecute;
      elements.toggleBoundary.disabled =
        !enabled || state.pendingPreview || state.pendingExecute || state.currentPage <= 1;
      elements.refreshPreview.disabled =
        !enabled || !state.boundaries.length || state.pendingPreview || state.pendingExecute;
      elements.prevPage.disabled = !enabled || state.currentPage <= 1;
      elements.nextPage.disabled =
        !enabled || state.currentPage >= (selected ? selected.page_count : 1);
      updateBoundaryMarker();
    }

    function renderDocumentList() {
      elements.docList.replaceChildren();
      if (!state.documents.length) {
        const empty = doc.createElement("p");
        setText(empty, "No documents are currently eligible for splitting.");
        elements.docList.appendChild(empty);
        return;
      }
      state.documents.forEach((item) => {
        const button = doc.createElement("button");
        button.type = "button";
        button.className = "document-choice";
        if (item.id === state.currentDocumentId) button.classList.add("selected");
        setText(button, `${item.label} (${item.page_count} pages)`);
        button.addEventListener("click", () => selectDocument(item.id));
        elements.docList.appendChild(button);
      });
    }

    function showCurrentPage() {
      const selected = selectedDocument();
      if (!selected) {
        elements.pdfFrame.src = "about:blank";
        updateViewerUi();
        return;
      }
      state.currentPage = clampPage(state.currentPage, selected.page_count);
      elements.pdfFrame.src = pdfUrl(selected.id, state.currentPage);
      updateViewerUi();
    }

    async function selectDocument(documentId) {
      const selected = state.documents.find((item) => item.id === documentId);
      if (!selected || state.pendingExecute) return;
      invalidatePreview();
      state.currentDocumentId = documentId;
      state.currentPage = 1;
      state.boundaries = [];
      setText(elements.previewList, "Choose a page and mark a split before it.");
      setMessage("");
      renderDocumentList();
      showCurrentPage();
    }

    async function loadDocuments() {
      const response = await fetchImpl("/api/split/documents", {
        headers: { "X-Aktenfux-CSRF": csrfToken },
      });
      if (!response.ok) throw new Error("Could not load documents.");
      const documents = await response.json();
      state.documents = documents;
      const retained = documents.some((item) => item.id === state.currentDocumentId);
      if (!retained) {
        invalidatePreview();
        state.currentDocumentId = documents[0]?.id || null;
        state.currentPage = 1;
        state.boundaries = [];
      }
      renderDocumentList();
      showCurrentPage();
      if (!documents.length) {
        setText(elements.previewList, "Select a document to preview.");
      }
    }

    async function preview() {
      const selected = selectedDocument();
      if (!selected || !state.boundaries.length || state.pendingExecute) return;
      abortPreview();
      const controller = new AbortController();
      state.previewController = controller;
      const sequence = state.previewSequence + 1;
      state.previewSequence = sequence;
      state.pendingPreview = true;
      state.preview = null;
      state.previewToken = null;
      clearConfirmation();
      updateViewerUi();
      try {
        const response = await fetchImpl("/api/split/preview", {
          method: "POST",
          headers: requestHeaders(),
          body: JSON.stringify({
            document_id: selected.id,
            boundaries: [...state.boundaries].sort((a, b) => a - b),
          }),
          signal: controller.signal,
        });
        const body = await response.json();
        if (sequence !== state.previewSequence) return;
        if (!response.ok) throw new Error(body.detail || "Invalid split preview.");
        state.preview = body.plan;
        state.previewToken = body.preview_token;
        renderPreviewParts(elements.previewList, body.plan, doc.createElement.bind(doc));
        setMessage(`Preview ready for ${body.plan.document_id}.`, "ok");
      } catch (error) {
        if (error && error.name === "AbortError") return;
        if (sequence !== state.previewSequence) return;
        setText(elements.previewList, "Preview failed.");
        setMessage(error?.message || "Preview failed.", "error");
        state.preview = null;
        state.previewToken = null;
      } finally {
        if (sequence === state.previewSequence) {
          state.pendingPreview = false;
          state.previewController = null;
          updateViewerUi();
        }
      }
    }

    async function executePlan() {
      const payload = buildExecutePayload(state);
      if (!payload) return;
      state.pendingExecute = true;
      clearConfirmation();
      updateViewerUi();
      try {
        const response = await fetchImpl("/api/split/execute", {
          method: "POST",
          headers: requestHeaders(),
          body: JSON.stringify(payload),
        });
        const body = await response.json();
        if (!response.ok) throw new Error(body.detail || "Split execution failed.");
        setMessage(`Split complete: ${body.result.outputs.length} parts created.`, "ok");
        invalidatePreview();
        state.boundaries = [];
        state.currentPage = 1;
        await loadDocuments();
      } catch (error) {
        invalidatePreview();
        setText(elements.previewList, "Preview again before retrying.");
        setMessage(error?.message || "Split execution failed.", "error");
      } finally {
        state.pendingExecute = false;
        updateViewerUi();
      }
    }

    function toggleBoundary() {
      if (
        !state.currentDocumentId ||
        state.currentPage <= 1 ||
        state.pendingPreview ||
        state.pendingExecute
      ) {
        return;
      }
      const page = state.currentPage;
      state.boundaries = state.boundaries.includes(page)
        ? state.boundaries.filter((value) => value !== page)
        : [...state.boundaries, page].sort((a, b) => a - b);
      invalidatePlanState(state);
      clearConfirmation();
      updateViewerUi();
      if (state.boundaries.length) {
        preview();
      } else {
        setText(elements.previewList, "Choose a page and mark a split before it.");
      }
    }

    function navigate(delta) {
      const selected = selectedDocument();
      if (!selected) return;
      state.currentPage = movePage(state.currentPage, selected.page_count, delta);
      showCurrentPage();
    }

    elements.zoomIn.addEventListener("click", () => {
      state.zoom = Math.min(2.5, Number((state.zoom + 0.25).toFixed(2)));
      updateViewTransform();
    });
    elements.zoomOut.addEventListener("click", () => {
      state.zoom = Math.max(0.5, Number((state.zoom - 0.25).toFixed(2)));
      updateViewTransform();
    });
    elements.rotate.addEventListener("click", () => {
      state.rotation = (state.rotation + 90) % 360;
      updateViewTransform();
    });
    elements.prevPage.addEventListener("click", () => navigate(-1));
    elements.nextPage.addEventListener("click", () => navigate(1));
    elements.toggleBoundary.addEventListener("click", toggleBoundary);
    elements.refreshPreview.addEventListener("click", preview);
    elements.confirmCheck.addEventListener("change", () => {
      state.confirmChecked = elements.confirmCheck.checked;
      updateViewerUi();
    });
    elements.executeButton.addEventListener("click", executePlan);
    doc.addEventListener("keydown", (event) => {
      if (event.key.toLowerCase() === "t" && event.target === doc.body) {
        toggleBoundary();
      }
    });

    updateViewTransform();
    updateViewerUi();
    loadDocuments().catch((error) => {
      setMessage(error?.message || "Could not load documents.", "error");
    });

    return { state, loadDocuments, preview, executePlan, selectDocument };
  }

  return {
    boundaryPresentation,
    buildExecutePayload,
    canExecute,
    clampPage,
    invalidatePlanState,
    movePage,
    pdfUrl,
    renderPreviewParts,
    setText,
    startWorkbench,
    viewerTransform,
  };
});
