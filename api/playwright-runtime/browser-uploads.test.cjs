"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");
const { approvedTransferFiles } = require("./browser-files.cjs");
const { NativeBrowserDownloads } = require("./browser-native-downloads.cjs");

function fixture() {
  const events = [], chunks = [];
  const element = { nodeType: 1, tagName: "INPUT", type: "file", multiple: true, isConnected: true,
    ownerDocument: { URL: "https://upload.example/form" }, dispatchEvent: e => events.push(e.type) };
  class DataTransfer {
    constructor() { this.files = []; this.items = { add: file => this.files.push(file) }; }
  }
  class File {
    constructor(parts, name, options) { this.bytes = Buffer.concat(parts.map(p => Buffer.from(p))); this.name = name; this.type = options.type; }
  }
  class Event { constructor(type) { this.type = type; } }
  const evaluate = (fn, first, second) => vm.runInNewContext(`(${fn})(first, second)`,
    { first, second, DataTransfer, File, Event, DragEvent: Event, Uint8Array, atob });
  const target = {
    evaluate: async (fn, arg) => evaluate(fn, element, arg),
    evaluateHandle: async fn => {
      const state = evaluate(fn, element);
      return { evaluate: async (fn, arg) => { if (typeof arg === "string") chunks.push(arg); return evaluate(fn, state, arg); }, dispose: async () => {} };
    },
    setInputFiles: () => assert.fail("Native upload must never bypass the approval adapter"),
    waitFor: async () => {},
  };
  return { element, target, events, chunks };
}

test("upload freezes sandbox bytes, waits before browser IO and returns exact approved hashes", async t => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "upload-test-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const filename = path.join(dir, "sample.bin"), original = Buffer.alloc(400001, 137);
  await fs.writeFile(filename, original);
  const f = fixture();
  const result = await approvedTransferFiles(f.target, [filename], { tabId: "tab_test", requestTransfer: async manifest => {
    assert.equal(f.chunks.length, 0); assert.equal(f.events.length, 0);
    assert.equal(manifest.direction, "upload");
    assert.equal(manifest.destination, f.element.ownerDocument.URL);
    assert.equal(manifest.files[0].sha256, crypto.createHash("sha256").update(original).digest("hex"));
    await fs.writeFile(filename, "changed while the user was reading approval");
    return { status: "approved" };
  } });
  assert.deepEqual(f.element.files[0].bytes, original);
  assert.equal(result.files[0].bytes, original.length);
  assert.deepEqual(f.events, ["input", "change"]);
});

for (const status of ["denied", "cancelled", "expired"]) test(`FilePayload ${status} sends no bytes or events`, async () => {
  const f = fixture();
  await assert.rejects(approvedTransferFiles(f.target, { name: "a.txt", mimeType: "text/plain", buffer: Buffer.from("private") }, {
    tabId: "tab_test", requestTransfer: async () => ({ status }) }), { code: "approval_" + status });
  assert.equal(f.chunks.length, 0); assert.equal(f.events.length, 0); assert.equal(f.element.files, undefined);
});

test("approval cannot drift to a navigated document", async () => {
  const f = fixture();
  await assert.rejects(approvedTransferFiles(f.target, { name: "a", buffer: Buffer.from("a") }, {
    tabId: "tab_test", requestTransfer: async () => { f.element.ownerDocument.URL = "https://different.example"; return { status: "approved" }; }
  }), { code: "upload_target_changed" });
  assert.equal(f.chunks.length, 0); assert.equal(f.events.length, 0);
});

test("iframe approval identifies its top-level website and keeps the actual document pinned", async () => {
  const f = fixture();
  f.element.ownerDocument.URL = "about:srcdoc";
  f.target.ownerFrame = async () => ({ page: () => ({ url: () => "https://upload.example/form" }) });
  await approvedTransferFiles(f.target, { name: "a", buffer: Buffer.from("a") }, {
    tabId: "tab_test", requestTransfer: async manifest => {
      assert.equal(manifest.destination, "about:srcdoc (within https://upload.example/form)");
      assert.equal(f.events.length, 0);
      return { status: "approved" };
    },
  });
  assert.deepEqual(f.events, ["input", "change"]);
});

test("iframe upload rejects a changed top-level page while approval is pending", async () => {
  const f = fixture();
  let pageUrl = "https://upload.example/form";
  f.element.ownerDocument.URL = "about:srcdoc";
  f.target.ownerFrame = async () => ({ page: () => ({ url: () => pageUrl }) });
  await assert.rejects(approvedTransferFiles(f.target, { name: "a", buffer: Buffer.from("a") }, {
    tabId: "tab_test", requestTransfer: async () => { pageUrl = "https://other.example"; return { status: "approved" }; },
  }), { code: "upload_target_changed" });
  assert.equal(f.chunks.length, 0); assert.equal(f.events.length, 0);
});

test("drop waits for approval and clearing a file input does not prompt", async () => {
  const f = fixture();
  await approvedTransferFiles(f.target, [{ name: "a", buffer: Buffer.from("a") }], {
    tabId: "tab_test", drop: true, requestTransfer: async () => { assert.equal(f.events.length, 0); return { status: "approved" }; }
  });
  assert.deepEqual(f.events, ["dragenter", "dragover", "drop"]);
  await approvedTransferFiles(f.target, [], { requestTransfer: () => assert.fail("clear needs no approval") });
  assert.equal(f.element.files.length, 0);
});

test("run-code upload entry points, including FilePayload and another page, all use their owned adapter", async () => {
  const f = fixture();
  const page = { frames: () => [], mainFrame: () => frame, locator: () => f.target, getByLabel: () => f.target,
    setInputFiles: f.target.setInputFiles, waitForEvent: async () => chooser };
  const frame = { parentFrame: () => null, page: () => page, locator: () => f.target, setInputFiles: f.target.setInputFiles };
  const chooser = { element: () => f.target, setFiles: f.target.setInputFiles };
  f.target.page = () => page;
  f.target.ownerFrame = async () => frame;
  f.target.elementHandle = async () => f.target;
  const popup = { ...page };
  page.context = () => ({ pages: () => [page, popup] });
  const calls = [];
  const other = { upload: async (target, files) => calls.push({ owner: "popup", target, files }) };
  const owner = new NativeBrowserDownloads({ page, id: "tab_test" }, () => {}, { downloadsForPage: async p => { assert.equal(p, popup); return other; } });
  owner.upload = async (target, files) => calls.push({ owner: "main", target, files });
  const proxy = await owner.scriptPage(), payload = { name: "a", buffer: Buffer.from("a") };
  for (const send of [
    () => proxy.setInputFiles("#input", payload),
    () => proxy.mainFrame().setInputFiles("#input", payload),
    () => proxy.getByLabel("input").setInputFiles(payload),
    async () => (await proxy.getByLabel("input").elementHandle()).setInputFiles(payload),
    async () => (await proxy.waitForEvent("filechooser")).setFiles(payload),
    () => proxy.context().pages()[1].setInputFiles("#input", payload),
  ]) assert.equal(await send(), undefined);
  assert.equal(calls.length, 6);
  assert.deepEqual(calls.map(c => c.owner), ["main", "main", "main", "main", "main", "popup"]);
  assert.ok(calls.every(c => c.files === payload));
});
