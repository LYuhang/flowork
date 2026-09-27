"use strict";

// Regression tests for the deployed native file path, not retired interception.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const { NativeBrowserDownloads } = require("./browser-native-downloads.cjs");
const { copyArtifact } = require("./browser-script-page.cjs");
const { artifactWriter } = require("./browser-files.cjs");

async function fixture(t, bytes = Buffer.from("native bytes")) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "browser-proxy-native-"));
  const commands = [], approvals = [], artifacts = [];
  const frame = { isDetached: () => false, parentFrame: () => null };
  const page = new EventEmitter();
  page.frames = () => [frame];
  page.mainFrame = () => frame;
  page.url = () => "https://example.test";
  page.context = () => "context";
  frame.page = () => page;
  let triggered = false;
  const cdp = { send: async (method, params) => {
    commands.push({ method, params });
    if (method === "Flowork.downloadBegin") return { capture_id: "capture", status: "watching" };
    if (method === "Flowork.downloadInfo") return triggered
      ? { status: "ready", choice_set_id: "set", candidates: [{ status: "ready", candidate_id: "candidate",
          name: "download.bin", bytes: bytes.length, source: "https://example.test" }] }
      : { status: "watching" };
    if (method === "Flowork.downloadRead") {
      assert.equal(params.transfer_id, "approved");
      return params.offset === 0 ? { data: bytes.toString("base64"), offset: bytes.length, eof: false }
        : { data: "", offset: bytes.length, eof: true };
    }
    if (method === "Flowork.downloadEnd") return {};
    assert.fail("Unexpected non-native download command: " + method);
  } };
  const owner = new NativeBrowserDownloads({ page, cdp, id: "tab_test" }, () => {}, {
    newArtifact: async () => path.join(root, "download"),
    requestTransfer: async input => { approvals.push(input); return { status: "approved", transfer_id: "approved" }; },
    recordArtifact: item => artifacts.push(item),
  });
  const trigger = async () => { assert.ok(owner.active); triggered = true; };
  page.click = frame.click = trigger;
  t.after(async () => { await owner.close({ turnEnd: true }); await fs.rm(root, { recursive: true, force: true }); });
  return { root, owner, page, frame, cdp, commands, approvals, artifacts, bytes, trigger };
}

test("run-code download saveAs copies only approved sandbox bytes and records every output", async t => {
  const f = await fixture(t);
  const proxy = await f.owner.scriptPage();
  const waiting = proxy.waitForEvent("download");
  await proxy.click("#download");
  const download = await waiting;
  assert.equal(download.suggestedFilename(), "download.bin");
  assert.equal(download.page(), proxy);
  assert.equal(await download.failure(), null);
  assert.equal(f.approvals.length, 0, "Observing the local download does not approve its transfer");
  await assert.rejects(fs.stat(path.join(f.root, "download")), { code: "ENOENT" });
  const first = await download.saveAs(path.join(f.root, "saved.bin"));
  const copy = await download.saveAs(path.join(f.root, "copied.bin"));
  assert.deepEqual(await fs.readFile(first.file), f.bytes);
  assert.deepEqual(await fs.readFile(copy.file), f.bytes);
  assert.equal(f.approvals.length, 1);
  assert.deepEqual(f.artifacts.map(a => a.file), [first.file, copy.file]);
  assert.equal(f.commands.filter(c => c.method === "Flowork.downloadBegin").length, 1);
});

test("context-page downloads prepare the child's native observer, not the parent's", async t => {
  const root = await fixture(t), child = await fixture(t, Buffer.from("child bytes"));
  const context = { pages: () => [root.page, child.page] };
  root.page.context = child.page.context = () => context;
  root.owner.downloadsForPage = async page => { assert.equal(page, child.page); return child.owner; };
  const proxy = await root.owner.scriptPage();
  const popup = proxy.context().pages()[1];
  const waiting = popup.waitForEvent("download");
  await popup.click("#download");
  const download = await waiting;
  assert.equal(root.owner.active, null);
  const file = await download.path();
  assert.deepEqual(await fs.readFile(file), child.bytes);
  assert.equal(child.approvals.length, 1);
  assert.equal(root.approvals.length, 0);
  await root.owner.close();
  assert.equal(child.owner.active, null);
});

test("download.page() remains on the approved upload adapter", async t => {
  const f = await fixture(t);
  const target = { waitFor: async () => {} };
  f.page.locator = () => target;
  f.page.setInputFiles = () => assert.fail("Download.page must not expose a raw upload bypass");
  const uploads = [];
  f.owner.upload = async (element, files) => uploads.push({ element, files });
  const proxy = await f.owner.scriptPage();
  const waiting = proxy.waitForEvent("download");
  await proxy.click("#download");
  const download = await waiting;
  const payload = { name: "payload.txt", buffer: Buffer.from("payload") };
  await download.page().setInputFiles("input", payload);
  assert.deepEqual(uploads, [{ element: target, files: payload }]);
  await download.cancel();
});

test("initialization failure rejects the original waiter and does not trigger a mutation", async t => {
  const f = await fixture(t);
  f.cdp.send = async method => {
    assert.equal(method, "Flowork.downloadBegin");
    throw new Error("native observation unavailable");
  };
  f.page.click = () => assert.fail("An unarmed observer must not click");
  const proxy = await f.owner.scriptPage();
  const waiting = proxy.waitForEvent("download");
  await assert.rejects(proxy.click("#download"), /native observation unavailable/);
  await assert.rejects(waiting, /native observation unavailable/);
  await f.owner.close();
  assert.deepEqual(await fs.readdir(f.root), []);
});

test("script exit cancels an unconsumed child observer and removes only staging files", async t => {
  const root = await fixture(t), child = await fixture(t);
  root.page.context = () => ({ pages: () => [child.page] });
  root.owner.downloadsForPage = async () => child.owner;
  const proxy = await root.owner.scriptPage();
  const waiting = proxy.context().pages()[0].waitForEvent("download", { timeout: 0 });
  await root.owner.close();
  await assert.rejects(waiting, { code: "download_cancelled" });
  assert.equal(child.owner.active, null);
  assert.deepEqual(await fs.readdir(child.root), []);
  assert.equal(child.approvals.length, 0);
});

test("frame actions honor the observer barrier and a read stream uses approved bytes", async t => {
  const f = await fixture(t);
  f.page.frame = () => f.frame;
  const proxy = await f.owner.scriptPage();
  const waiting = proxy.waitForEvent("download");
  await proxy.frame("child").click("#download");
  const download = await waiting;
  const chunks = [];
  for await (const part of await download.createReadStream()) chunks.push(part);
  assert.deepEqual(Buffer.concat(chunks), f.bytes);
  assert.equal(f.approvals.length, 1);
});

test("concurrent native observers never orphan another writer", async t => {
  const f = await fixture(t);
  const first = f.owner.arm({ file: path.join(f.root, "first"), timeout: 0 });
  await assert.rejects(f.owner.arm({ file: path.join(f.root, "second"), timeout: 0 }), { code: "download_pending" });
  await first;
  await f.owner.close();
  assert.deepEqual(await fs.readdir(f.root), []);
  assert.equal(f.commands.filter(c => c.method === "Flowork.downloadBegin").length, 1);
});

test("an unknown popup cannot fall back to native unapproved FilePayload upload", async t => {
  const f = await fixture(t), other = await fixture(t);
  other.page.setInputFiles = () => assert.fail("No upload fallback");
  other.page.locator = () => ({ waitFor: async () => {} });
  f.page.context = () => ({ pages: () => [other.page] });
  const proxy = await f.owner.scriptPage();
  await assert.rejects(proxy.context().pages()[0].setInputFiles("input", {
    name: "secret", buffer: Buffer.from("secret"),
  }), { code: "tab_unavailable" });
  assert.equal(other.approvals.length, 0);
});

test("stream writer commits exact binary bytes and abort removes staging files", async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "browser-stream-unit-"));
  try {
    const file = path.join(directory, "binary");
    const bytes = Buffer.from([0, 255, 128, 13, 10]);
    const writer = await artifactWriter(file);
    await writer.write(bytes.subarray(0, 2));
    await writer.write(bytes.subarray(2));
    await assert.rejects(fs.stat(file), { code: "ENOENT" });
    const artifact = await writer.commit();
    assert.deepEqual(artifact, { file, bytes: bytes.length, sha256: crypto.createHash("sha256").update(bytes).digest("hex") });
    assert.deepEqual(await fs.readFile(file), bytes);
    assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
    const aborted = await artifactWriter(path.join(directory, "partial"));
    await aborted.write(bytes);
    await aborted.abort();
    assert.deepEqual(await fs.readdir(directory), ["binary"]);
    const copy = path.join(directory, "copy");
    assert.equal((await copyArtifact(file, copy)).sha256, artifact.sha256);
    await assert.rejects(copyArtifact(file, copy), { code: "file_exists" });
  } finally { await fs.rm(directory, { recursive: true, force: true }); }
});

test("stream commit refuses a concurrent destination or symlink without clobbering it", async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "browser-stream-race-"));
  try {
    const file = path.join(directory, "result");
    const writer = await artifactWriter(file);
    await writer.write(Buffer.from("new"));
    await fs.symlink(path.join(directory, "not-created"), file);
    await assert.rejects(writer.commit(), { code: "file_exists" });
    assert.deepEqual(await fs.readdir(directory), ["result"]);
    assert.equal(await fs.readlink(file), path.join(directory, "not-created"));
  } finally { await fs.rm(directory, { recursive: true, force: true }); }
});

test("run-code proxy preserves synchronous Playwright queries and locator factories", async t => {
  const { owner, page } = await fixture(t);
  const locator = { count: async () => 2, first() { return this; } };
  page.getByRole = () => locator;
  try {
    const proxy = await owner.scriptPage();
    assert.equal(proxy.url(), "https://example.test");
    assert.equal(proxy.context(), "context");
    assert.equal(await proxy.getByRole("button").first().count(), 2);
  } finally { await owner.close(); }
});

test("run-code event predicates and results preserve wrapped Frame identity", async t => {
  const { owner, page, frame } = await fixture(t);
  page.waitForEvent = async (event, options) => {
    assert.equal(event, "framedetached");
    const predicate = typeof options === "function" ? options : options.predicate;
    if (predicate) assert.equal(await predicate(frame), true);
    if (typeof options !== "function") assert.equal(options.timeout, 1234);
    return frame;
  };
  try {
    const proxy = await owner.scriptPage();
    const expected = proxy.frames()[0];
    assert.equal(await proxy.waitForEvent("framedetached", {
      predicate: candidate => candidate === expected, timeout: 1234,
    }), expected);
    assert.equal(await proxy.waitForEvent("framedetached", async candidate => candidate === expected), expected);
    assert.equal(await proxy.waitForEvent("framedetached", { timeout: 1234 }), expected);
  } finally { await owner.close(); }
});

test("script popup listeners preserve identity and removal", async t => {
  const { owner, page } = await fixture(t);
  const popup = (await fixture(t)).page;
  page.waitForEvent = async () => popup;
  try {
    const proxy = await owner.scriptPage();
    const wrapped = await proxy.waitForEvent("popup");
    let received;
    const listener = value => { received = value; };
    assert.equal(proxy.on("popup", listener), proxy);
    page.emit("popup", popup);
    assert.equal(received, wrapped);
    proxy.off("popup", listener);
    assert.equal(page.listenerCount("popup"), 0);
  } finally { await owner.close(); }
});
