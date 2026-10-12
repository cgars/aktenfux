"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const workbench = require("../aktenfux/workbench.js");

class FakeNode {
  constructor() {
    this.children = [];
    this.className = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.listeners = new Map();
    this.src = "";
    this.style = {};
    this.textContent = "";
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
  ];
  const nodes = Object.fromEntries(ids.map((id) => [id, new FakeNode()]));
  return {
    body: new FakeNode(),
    nodes,
    addEventListener() {},
    createElement() {
      return new FakeNode();
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

test("page navigation changes the PDF fragment and stays bounded", () => {
  assert.equal(workbench.movePage(1, 8, -1), 1);
  assert.equal(workbench.movePage(1, 8, 1), 2);
  assert.equal(workbench.movePage(8, 8, 1), 8);
  assert.equal(
    workbench.pdfUrl("doc 001", 2),
    "/api/split/documents/doc%20001/pdf#page=2"
  );
});

test("zoom and rotation produce one transform on the PDF frame", () => {
  assert.equal(workbench.viewerTransform(1.25, 90), "scale(1.25) rotate(90deg)");
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

  assert.equal(doc.nodes["pdf-frame"].src.endsWith("/doc001/pdf#page=1"), true);
  doc.nodes["next-page"].click();
  assert.equal(doc.nodes["pdf-frame"].src.endsWith("/doc001/pdf#page=2"), true);

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
  assert.equal(doc.nodes["pdf-frame"].src.endsWith("/doc002/pdf#page=1"), true);
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
