"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const workbench = require("../aktenfux/workbench.js");

class FakeNode {
  constructor(tagName = "div") {
    this.children = [];
    this.className = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.isContentEditable = false;
    this.listeners = new Map();
    this.src = "";
    this.style = {};
    this.tagName = tagName.toUpperCase();
    this.textContent = "";
    this.focusCalls = 0;
    this.classList = {
      add: (...names) => {
        this.className = [...new Set(this.className.split(" ").filter(Boolean).concat(names))].join(" ");
      },
    };
  }

  addEventListener(name, listener) {
    this.listeners.set(name, listener);
  }

  appendChild(child) {
    this.children.push(child);
  }

  click() {
    this.listeners.get("click")?.({ target: this });
  }

  focus() {
    this.focusCalls += 1;
  }

  replaceChildren(...children) {
    this.children = children;
  }
}

function fakeDocument() {
  const ids = [
    "document-list",
    "preview-list",
    "pdf-frame",
    "page-info",
    "execute",
    "confirm-check",
    "message",
    "boundary-marker",
    "prev-page",
    "next-page",
    "zoom-out",
    "zoom-in",
    "rotate",
    "toggle-boundary",
    "refresh-preview",
    "suggest-boundaries",
    "apply-suggestions",
    "suggestion-status",
    "suggestion-list",
  ];
  const nodes = Object.fromEntries(ids.map((id) => [id, new FakeNode()]));
  const listeners = new Map();
  const viewListeners = new Map();
  const body = new FakeNode("body");
  return {
    activeElement: body,
    body,
    defaultView: {
      addEventListener(name, listener) {
        viewListeners.set(name, listener);
      },
      dispatch(name) {
        viewListeners.get(name)?.();
      },
    },
    nodes,
    addEventListener(name, listener) {
      listeners.set(name, listener);
    },
    createElement(tagName) {
      return new FakeNode(tagName);
    },
    dispatch(name, event) {
      listeners.get(name)?.(event);
    },
    getElementById(id) {
      return nodes[id];
    },
    querySelector(selector) {
      assert.equal(selector, 'meta[name="aktenfux-csrf-token"]');
      return { content: "test-token" };
    },
  };
}

function jsonResponse(ok, body) {
  return { ok, async json() { return body; } };
}

async function flushTasks() {
  await new Promise((resolve) => setImmediate(resolve));
}

function executableState(overrides = {}) {
  return {
    currentDocumentId: "doc001",
    preview: { operation_id: "operation" },
    previewToken: "a".repeat(64),
    previewSequence: 4,
    confirmChecked: true,
    pendingPreview: false,
    pendingExecute: false,
    ...overrides,
  };
}

test("page navigation requests one exact preview page and stays bounded", () => {
  assert.equal(workbench.movePage(1, 8, -1), 1);
  assert.equal(workbench.movePage(1, 8, 1), 2);
  assert.equal(workbench.movePage(8, 8, 1), 8);
  assert.equal(
    workbench.pdfUrl("doc 001", 2),
    "/api/split/documents/doc%20001/pdf?page=2#page=1&toolbar=0&navpanes=0&scrollbar=0&view=Fit"
  );
});

test("zoom and rotation produce one transform on the PDF frame", () => {
  assert.equal(workbench.viewerTransform(1.25, 90), "scale(1.25) rotate(90deg)");
});

test("viewer focus returns to the workbench and T ignores editable fields", () => {
  const doc = fakeDocument();
  doc.activeElement = doc.nodes["pdf-frame"];

  assert.equal(workbench.restoreFocusFromViewer(doc, doc.nodes["pdf-frame"]), true);
  assert.equal(doc.body.focusCalls, 1);
  assert.equal(workbench.isEditableTarget(new FakeNode("input")), true);
  assert.equal(workbench.isEditableTarget(new FakeNode("textarea")), true);
  assert.equal(workbench.isEditableTarget(new FakeNode("button")), false);
});

test("boundary marker states the exact between-page consequence", () => {
  assert.deepEqual(workbench.boundaryPresentation([3, 6], 3), {
    visible: true,
    text: "Split before page 3",
  });
  assert.deepEqual(workbench.boundaryPresentation([3, 6], 4), {
    visible: false,
    text: "",
  });
  assert.equal(workbench.boundaryPresentation([1], 1).visible, false);
});

test("execution is disabled until the exact preview is confirmed and idle", () => {
  assert.equal(workbench.canExecute(executableState()), true);
  assert.equal(workbench.canExecute(executableState({ confirmChecked: false })), false);
  assert.equal(workbench.canExecute(executableState({ pendingPreview: true })), false);
  assert.equal(workbench.canExecute(executableState({ pendingExecute: true })), false);
  assert.equal(workbench.canExecute(executableState({ preview: null })), false);
});

test("execute payload carries an explicit literal confirmation", () => {
  const state = executableState();
  assert.deepEqual(workbench.buildExecutePayload(state), {
    document_id: "doc001",
    plan: state.preview,
    preview_token: state.previewToken,
    confirm: true,
  });
  assert.equal(
    workbench.buildExecutePayload(executableState({ confirmChecked: false })),
    null
  );
});

test("document or boundary changes invalidate confirmation and stale preview", () => {
  const state = executableState();
  workbench.invalidatePlanState(state);
  assert.equal(state.previewSequence, 5);
  assert.equal(state.preview, null);
  assert.equal(state.previewToken, null);
  assert.equal(state.confirmChecked, false);
});

test("hostile filenames are assigned as text rather than markup", () => {
  const children = [];
  const container = {
    replaceChildren() {
      children.length = 0;
    },
    appendChild(child) {
      children.push(child);
    },
  };
  const createElement = () => ({ textContent: "" });
  const hostile = '<img src=x onerror="globalThis.pwned=true">.pdf';

  workbench.renderPreviewParts(
    container,
    { parts: [{ start_page: 1, end_page: 2, destination_filename: hostile }] },
    createElement
  );

  assert.equal(children.length, 1);
  assert.equal(children[0].textContent.includes(hostile), true);
  assert.equal(globalThis.pwned, undefined);
});

test("controller navigates the PDF and cancels a stale document preview", async () => {
  const doc = fakeDocument();
  const documents = [
    { id: "doc001", label: "First", page_count: 4 },
    { id: "doc002", label: "Second", page_count: 3 },
  ];
  let previewAborted = false;
  const fetchImpl = (url, options = {}) => {
    if (url === "/api/split/documents") return Promise.resolve(jsonResponse(true, documents));
    if (url === "/api/split/preview") {
      return new Promise((resolve, reject) => {
        options.signal.addEventListener("abort", () => {
          previewAborted = true;
          const error = new Error("aborted");
          error.name = "AbortError";
          reject(error);
        });
      });
    }
    throw new Error(`unexpected request ${url}`);
  };
  const controller = workbench.startWorkbench(doc, fetchImpl);
  await flushTasks();

  assert.equal(
    doc.nodes["pdf-frame"].src.endsWith(
      "/doc001/pdf?page=1#page=1&toolbar=0&navpanes=0&scrollbar=0&view=Fit"
    ),
    true
  );
  doc.nodes["next-page"].click();
  assert.equal(
    doc.nodes["pdf-frame"].src.endsWith(
      "/doc001/pdf?page=2#page=1&toolbar=0&navpanes=0&scrollbar=0&view=Fit"
    ),
    true
  );
  assert.equal(doc.nodes["page-info"].textContent, "Document page 2/4");

  controller.state.boundaries = [2];
  controller.state.confirmChecked = true;
  const pendingPreview = controller.preview();
  await flushTasks();
  await controller.selectDocument("doc002");
  await pendingPreview;

  assert.equal(previewAborted, true);
  assert.equal(controller.state.currentDocumentId, "doc002");
  assert.equal(controller.state.preview, null);
  assert.equal(controller.state.previewToken, null);
  assert.equal(controller.state.confirmChecked, false);
  assert.equal(doc.nodes["confirm-check"].checked, false);
  assert.equal(
    doc.nodes["pdf-frame"].src.endsWith(
      "/doc002/pdf?page=1#page=1&toolbar=0&navpanes=0&scrollbar=0&view=Fit"
    ),
    true
  );
});

test("controller restores focus from the PDF and handles T outside form fields", async () => {
  const doc = fakeDocument();
  const documents = [{ id: "doc001", label: "First", page_count: 4 }];
  let previewCalls = 0;
  const fetchImpl = (url) => {
    if (url === "/api/split/documents") {
      return Promise.resolve(jsonResponse(true, documents));
    }
    if (url === "/api/split/preview") {
      previewCalls += 1;
      return Promise.resolve(jsonResponse(true, {
        plan: { document_id: "doc001", parts: [] },
        preview_token: "c".repeat(64),
      }));
    }
    throw new Error(`unexpected request ${url}`);
  };
  const controller = workbench.startWorkbench(doc, fetchImpl);
  await flushTasks();
  doc.nodes["next-page"].click();

  doc.activeElement = doc.nodes["pdf-frame"];
  doc.defaultView.dispatch("blur");
  assert.equal(doc.body.focusCalls, 1);

  doc.dispatch("keydown", { key: "t", target: doc.body });
  await flushTasks();
  assert.deepEqual(controller.state.boundaries, [2]);
  assert.equal(previewCalls, 1);

  doc.dispatch("keydown", { key: "t", target: new FakeNode("input") });
  assert.deepEqual(controller.state.boundaries, [2]);
  assert.equal(previewCalls, 1);
});

test("controller blocks duplicate execution and recovers through a fresh preview", async () => {
  const doc = fakeDocument();
  const documents = [{ id: "doc001", label: "First", page_count: 4 }];
  let executeCalls = 0;
  let resolveExecution;
  let previewCalls = 0;
  const fetchImpl = (url) => {
    if (url === "/api/split/documents") return Promise.resolve(jsonResponse(true, documents));
    if (url === "/api/split/execute") {
      executeCalls += 1;
      return new Promise((resolve) => { resolveExecution = resolve; });
    }
    if (url === "/api/split/preview") {
      previewCalls += 1;
      return Promise.resolve(jsonResponse(true, {
        plan: { document_id: "doc001", parts: [] },
        preview_token: "b".repeat(64),
      }));
    }
    throw new Error(`unexpected request ${url}`);
  };
  const controller = workbench.startWorkbench(doc, fetchImpl);
  await flushTasks();
  controller.state.preview = { document_id: "doc001", parts: [] };
  controller.state.previewToken = "a".repeat(64);
  controller.state.confirmChecked = true;
  controller.state.boundaries = [2];

  const first = controller.executePlan();
  const duplicate = controller.executePlan();
  assert.equal(executeCalls, 1);
  await duplicate;
  resolveExecution(jsonResponse(false, { detail: "<img src=x onerror=alert(1)>" }));
  await first;

  assert.equal(controller.state.pendingExecute, false);
  assert.equal(controller.state.preview, null);
  assert.equal(controller.state.previewToken, null);
  assert.equal(controller.state.confirmChecked, false);
  assert.equal(doc.nodes.message.textContent, "<img src=x onerror=alert(1)>");
  assert.equal(globalThis.pwned, undefined);

  await controller.preview();
  assert.equal(previewCalls, 1);
  assert.equal(controller.state.previewToken, "b".repeat(64));
  assert.equal(controller.state.confirmChecked, false);
});

test("suggestion rendering stays text-only and leaves candidates unselected", () => {
  const rows = [];
  const container = {
    replaceChildren() {
      rows.length = 0;
    },
    appendChild(child) {
      rows.push(child);
    },
  };
  const createElement = () => new FakeNode("div");
  const suggestion = {
    candidates: [
      {
        before_page: 4,
        confidence: 0.92,
        rationale: '<img src=x onerror="globalThis.pwned=true">',
        evidence: [{ page: 4, cue: "From: Example Corp" }],
      },
    ],
  };

  workbench.renderSuggestionCandidates(container, suggestion, createElement);

  assert.equal(rows.length, 1);
  assert.equal(rows[0].textContent.includes("Split before page 4"), true);
  assert.equal(globalThis.pwned, undefined);
});

test("applying selected suggestions invalidates stale preview state", () => {
  const state = executableState({
    boundaries: [2],
    suggestedBoundaries: [4, 6],
    selectedSuggestionBoundaries: new Set([6]),
  });

  workbench.applySelectedSuggestions(state);

  assert.deepEqual(state.boundaries, [2, 6]);
  assert.equal(state.preview, null);
  assert.equal(state.previewToken, null);
  assert.equal(state.confirmChecked, false);
});

test("controller suggestion pending disables duplicate submit and renders warnings without candidates", async () => {
  const doc = fakeDocument();
  const documents = [{ id: "doc001", label: "First", page_count: 4, source_sha256: "a".repeat(64) }];
  let resolveSuggest;
  let suggestCalls = 0;
  const fetchImpl = (url) => {
    if (url === "/api/split/documents") {
      return Promise.resolve(jsonResponse(true, documents));
    }
    if (url === "/api/split/suggest") {
      suggestCalls += 1;
      return new Promise((resolve) => { resolveSuggest = resolve; });
    }
    throw new Error(`unexpected request ${url}`);
  };
  const controller = workbench.startWorkbench(doc, fetchImpl);
  await flushTasks();

  doc.nodes["suggest-boundaries"].click();
  assert.equal(doc.nodes["suggest-boundaries"].disabled, true);
  doc.nodes["suggest-boundaries"].click();
  assert.equal(suggestCalls, 1);
  resolveSuggest(jsonResponse(true, {
    suggestion: {
      document_id: "doc001",
      source_sha256: "a".repeat(64),
      source_page_count: 4,
      suggestion_provider: "ollama:test",
      candidates: [],
      warnings: ["Page 1 had no extractable text."],
    },
  }));
  await flushTasks();

  assert.equal(doc.nodes["suggestion-list"].children.length, 2);
  assert.equal(doc.nodes["suggestion-list"].children[1].textContent.includes("Page 1 had no extractable text."), true);
  assert.equal(controller.state.selectedSuggestionBoundaries.size, 0);
});

test("controller suggestion response is ignored after document switch", async () => {
  const doc = fakeDocument();
  const documents = [
    { id: "doc001", label: "First", page_count: 4, source_sha256: "a".repeat(64) },
    { id: "doc002", label: "Second", page_count: 3, source_sha256: "b".repeat(64) },
  ];
  let rejectSuggest;
  const fetchImpl = (url, options = {}) => {
    if (url === "/api/split/documents") return Promise.resolve(jsonResponse(true, documents));
    if (url === "/api/split/suggest") {
      return new Promise((resolve, reject) => {
        options.signal.addEventListener("abort", () => {
          const error = new Error("aborted");
          error.name = "AbortError";
          reject(error);
        });
        rejectSuggest = reject;
      });
    }
    throw new Error(`unexpected request ${url}`);
  };
  const controller = workbench.startWorkbench(doc, fetchImpl);
  await flushTasks();

  doc.nodes["suggest-boundaries"].click();
  await flushTasks();
  await controller.selectDocument("doc002");
  await flushTasks();

  assert.equal(controller.state.currentDocumentId, "doc002");
  assert.equal(controller.state.suggestions, null);
  assert.equal(controller.state.pendingSuggest, false);
  assert.equal(rejectSuggest !== undefined, true);
});

test("controller applies only selected suggestions and triggers fresh preview", async () => {
  const doc = fakeDocument();
  const documents = [{ id: "doc001", label: "First", page_count: 8, source_sha256: "a".repeat(64) }];
  let previewCalls = 0;
  const fetchImpl = (url) => {
    if (url === "/api/split/documents") return Promise.resolve(jsonResponse(true, documents));
    if (url === "/api/split/suggest") {
      return Promise.resolve(jsonResponse(true, {
        suggestion: {
          document_id: "doc001",
          source_sha256: "a".repeat(64),
          source_page_count: 8,
          suggestion_provider: "ollama:test",
          candidates: [
            { before_page: 4, confidence: 0.8, rationale: "first", evidence: [] },
            { before_page: 6, confidence: 0.7, rationale: "second", evidence: [] },
          ],
          warnings: [],
        },
      }));
    }
    if (url === "/api/split/preview") {
      previewCalls += 1;
      return Promise.resolve(jsonResponse(true, {
        plan: { document_id: "doc001", parts: [] },
        preview_token: "d".repeat(64),
      }));
    }
    throw new Error(`unexpected request ${url}`);
  };
  const controller = workbench.startWorkbench(doc, fetchImpl);
  await flushTasks();

  doc.nodes["suggest-boundaries"].click();
  await flushTasks();

  const firstRow = doc.nodes["suggestion-list"].children[0];
  const firstCheckbox = firstRow.children[0];
  assert.equal(firstCheckbox.checked, false);
  firstCheckbox.checked = true;
  firstCheckbox.listeners.get("change")();
  controller.state.preview = { old: true };
  controller.state.previewToken = "e".repeat(64);
  controller.state.confirmChecked = true;
  doc.nodes["apply-suggestions"].click();
  await flushTasks();
  await flushTasks();

  assert.deepEqual(controller.state.boundaries, [4]);
  assert.equal(controller.state.previewToken, "d".repeat(64));
  assert.equal(controller.state.confirmChecked, false);
  assert.equal(previewCalls >= 1, true);
});

test("controller invalidates stale suggestion on same-id source change", async () => {
  const doc = fakeDocument();
  const initialDocuments = [{ id: "doc001", label: "First", page_count: 8, source_sha256: "a".repeat(64) }];
  const changedDocuments = [{ id: "doc001", label: "First", page_count: 8, source_sha256: "b".repeat(64) }];
  let listCalls = 0;
  const fetchImpl = (url) => {
    if (url === "/api/split/documents") {
      listCalls += 1;
      return Promise.resolve(jsonResponse(true, listCalls > 1 ? changedDocuments : initialDocuments));
    }
    if (url === "/api/split/suggest") {
      return Promise.resolve(jsonResponse(true, {
        suggestion: {
          document_id: "doc001",
          source_sha256: "a".repeat(64),
          source_page_count: 8,
          suggestion_provider: "ollama:test",
          candidates: [
            { before_page: 4, confidence: 0.8, rationale: "first", evidence: [] },
          ],
          warnings: [],
        },
      }));
    }
    throw new Error(`unexpected request ${url}`);
  };
  const controller = workbench.startWorkbench(doc, fetchImpl);
  await flushTasks();

  doc.nodes["suggest-boundaries"].click();
  await flushTasks();
  assert.equal(controller.state.suggestions !== null, true);
  const firstRow = doc.nodes["suggestion-list"].children[0];
  const firstCheckbox = firstRow.children[0];
  firstCheckbox.checked = true;
  firstCheckbox.listeners.get("change")();
  doc.nodes["apply-suggestions"].click();
  await flushTasks();

  assert.equal(controller.state.suggestions, null);
  assert.equal(doc.nodes["suggestion-status"].textContent.includes("stale"), true);
});

test("controller clears preview and suggestion after same-id source change post-preview", async () => {
  const doc = fakeDocument();
  const documentsV1 = [{ id: "doc001", label: "First", page_count: 8, source_sha256: "a".repeat(64) }];
  const documentsV2 = [{ id: "doc001", label: "First", page_count: 8, source_sha256: "c".repeat(64) }];
  let listCalls = 0;
  const fetchImpl = (url) => {
    if (url === "/api/split/documents") {
      listCalls += 1;
      return Promise.resolve(jsonResponse(true, listCalls >= 3 ? documentsV2 : documentsV1));
    }
    if (url === "/api/split/suggest") {
      return Promise.resolve(jsonResponse(true, {
        suggestion: {
          document_id: "doc001",
          source_sha256: "a".repeat(64),
          source_page_count: 8,
          suggestion_provider: "ollama:test",
          candidates: [{ before_page: 4, confidence: 0.8, rationale: "first", evidence: [] }],
          warnings: [],
        },
      }));
    }
    if (url === "/api/split/preview") {
      return Promise.resolve(jsonResponse(true, {
        plan: { document_id: "doc001", parts: [] },
        preview_token: "f".repeat(64),
      }));
    }
    throw new Error(`unexpected request ${url}`);
  };
  const controller = workbench.startWorkbench(doc, fetchImpl);
  await flushTasks();

  doc.nodes["suggest-boundaries"].click();
  await flushTasks();
  const firstCheckbox = doc.nodes["suggestion-list"].children[0].children[0];
  firstCheckbox.checked = true;
  firstCheckbox.listeners.get("change")();
  doc.nodes["apply-suggestions"].click();
  await flushTasks();
  await flushTasks();
  assert.equal(controller.state.previewToken, "f".repeat(64));

  await controller.loadDocuments();

  assert.equal(controller.state.preview, null);
  assert.equal(controller.state.previewToken, null);
  assert.equal(controller.state.suggestions, null);
});
