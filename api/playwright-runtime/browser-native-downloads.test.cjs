"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { NativeBrowserDownloads } = require("./browser-native-downloads.cjs");

async function fixture(t, { count = 1, decision = { status: "approved", transfer_id: "approved-call" }, badOffset = false } = {}) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "flowork-native-test-"));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const file = path.join(root, "result.bin");
  const bytes = Buffer.from([0, 1, 2, 127, 128, 254, 255]);
  const calls = [], approvals = [], artifacts = [];
  let triggered = false;
  const candidates = Array.from({ length: count }, (_, i) => ({ status: "ready", candidate_id: "candidate-" + i, name: `file-${i}.bin`, bytes: bytes.length, source: "https://example.com" }));
  const state = { page: {}, cdp: { send: async (method, params) => {
    calls.push({ method, params });
    if (method === "Flowork.downloadBegin") return { capture_id: "capture", status: "watching" };
    if (method === "Flowork.downloadInfo") return triggered ? { status: count > 1 ? "selection_required" : "ready", capture_id: "capture", choice_set_id: "set", tab_id: 10, candidates } : { status: "watching" };
    if (method === "Flowork.downloadRead") {
      assert.equal(params.transfer_id, "approved-call");
      return params.offset === 0 ? { data: bytes.toString("base64"), offset: badOffset ? 3 : bytes.length, eof: false } : { data: "", offset: bytes.length, eof: true };
    }
    if (method === "Flowork.downloadEnd") return { ended: true };
    throw new Error("Unexpected method: " + method);
  } } };
  const options = { requestTransfer: async input => { approvals.push(input); return typeof decision === "function" ? decision() : decision; }, recordArtifact: a => artifacts.push(a) };
  const adapter = new NativeBrowserDownloads(state, () => {}, options);
  return { adapter, root, file, bytes, calls, approvals, artifacts, trigger: async () => { triggered = true; } };
}

test("native download writes exact binary bytes only after approval and never intercepts a response", async t => {
  let allow;
  const f = await fixture(t, { decision: () => new Promise(resolve => { allow = resolve; }) });
  const run = f.adapter.capture({ file: f.file, timeout: 1 }, f.trigger);
  while (!allow) await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(f.calls.some(c => c.method === "Flowork.downloadRead"), false);
  await assert.rejects(fs.stat(f.file), { code: "ENOENT" });
  allow({ status: "approved", transfer_id: "approved-call" });
  const result = await run;
  assert.deepEqual(await fs.readFile(f.file), f.bytes);
  assert.equal(result.bytes, f.bytes.length);
  assert.match(result.sha256, /^[a-f0-9]{64}$/);
  assert.equal(f.artifacts.length, 1);
  assert.equal(f.calls.at(-1).method, "Flowork.downloadEnd");
  assert.equal(f.calls.some(c => /Fetch\.|Network\.|download\.delete/.test(c.method)), false);
});

test("denial releases capture without reading bytes or leaving a sandbox file", async t => {
  const f = await fixture(t, { decision: { status: "denied", message: "The user denied file transfer." } });
  await assert.rejects(f.adapter.capture({ file: f.file, timeout: 1 }, f.trigger), { code: "approval_denied" });
  assert.equal(f.calls.some(c => c.method === "Flowork.downloadRead"), false);
  assert.deepEqual(await fs.readdir(f.root), []);
  assert.equal(f.adapter.active, null);
});

test("local confirmation waits beyond start timeout, reports progress once and never starts transfer before confirmation", async t => {
  const f = await fixture(t);
  const original = f.adapter.state.cdp.send;
  let confirmed = false, pending = false;
  const progress = [];
  f.adapter.progress = value => progress.push(value);
  f.adapter.state.cdp.send = async (method, params) => {
    if (method === "Flowork.downloadInfo" && !confirmed) {
      pending = true;
      return { status: "local_confirmation_required", capture_id: "capture" };
    }
    return original(method, params);
  };
  const run = f.adapter.capture({ file: f.file, timeout: 0.001 }, f.trigger);
  while (!pending) await new Promise(resolve => setTimeout(resolve, 10));
  await new Promise(resolve => setTimeout(resolve, 350));
  assert.deepEqual(f.approvals, []);
  assert.equal(progress.filter(p => p.status === "waiting_for_local_confirmation").length, 1);
  await assert.rejects(fs.stat(f.file), { code: "ENOENT" });
  confirmed = true;
  await run;
  assert.deepEqual(await fs.readFile(f.file), f.bytes);
});

test("ending the turn removes a pending local confirmation without transferring bytes", async t => {
  const f = await fixture(t);
  const original = f.adapter.state.cdp.send;
  let pending = false;
  f.adapter.state.cdp.send = async (method, params) => {
    if (method === "Flowork.downloadInfo") { pending = true; return { status: "local_confirmation_required" }; }
    return original(method, params);
  };
  const run = f.adapter.capture({ file: f.file }, f.trigger);
  const rejected = assert.rejects(run, { code: "download_cancelled" });
  while (!pending) await new Promise(resolve => setTimeout(resolve, 10));
  await f.adapter.close({ turnEnd: true }); await rejected;
  assert.equal(f.adapter.active, null); assert.deepEqual(f.approvals, []);
  assert.deepEqual(await fs.readdir(f.root), []);
  assert.ok(f.calls.some(c => c.method === "Flowork.downloadEnd"));
});

test("local cancellation has an explicit Agent-facing code and no-retry hint", async t => {
  const f = await fixture(t);
  const original = f.adapter.state.cdp.send;
  f.adapter.state.cdp.send = async (method, params) => {
    if (method === "Flowork.downloadInfo") throw new Error("Protocol error: download_local_cancelled: User cancelled");
    return original(method, params);
  };
  await assert.rejects(f.adapter.capture({ file: f.file }, f.trigger), error => {
    assert.equal(error.code, "download_local_cancelled");
    assert.match(error.hint, /Do not repeat/); return true;
  });
  assert.deepEqual(f.approvals, []); assert.deepEqual(await fs.readdir(f.root), []);
});

test("ambiguous files survive across commands; selection and transfer permission remain separate", async t => {
  let selected = false;
  const f = await fixture(t, { count: 2, decision: () => selected ? { status: "approved", transfer_id: "approved-call" } : { status: "selection_required", message: "Wait for the user's choice." } });
  const result = await f.adapter.capture({ file: f.file, timeout: 1 }, f.trigger);
  assert.equal(result.status, "selection_required");
  assert.equal(Object.hasOwn(result, "tab_id"), false);
  assert.equal(f.approvals.length, 0);
  assert.deepEqual(await fs.readdir(f.root), []);
  await f.adapter.close(); // End of a CLI script is not end of the Agent turn.
  const status = await f.adapter.status("set");
  assert.equal(status.candidates.length, 2);
  assert.equal(Object.hasOwn(status, "tab_id"), false);
  await assert.rejects(f.adapter.receive("set", "candidate-1", f.file), { code: "download_selection_required" });
  assert.notEqual(f.adapter.active, null);
  selected = true;
  await f.adapter.receive("set", "candidate-1", f.file);
  assert.equal(f.approvals.at(-1).candidate_id, "candidate-1");
  assert.equal(f.calls.filter(c => c.method === "Flowork.downloadBegin").length, 1);
  assert.deepEqual(await fs.readFile(f.file), f.bytes);
});

test("cancel/turn end invalidate pending captures without consuming the local file", async t => {
  const f = await fixture(t, { count: 2 });
  await f.adapter.capture({ file: f.file, timeout: 1 }, f.trigger);
  await f.adapter.close({ turnEnd: true });
  assert.equal(f.calls.some(c => c.method === "Flowork.downloadRead"), false);
  await assert.rejects(f.adapter.receive("set", "candidate-0", f.file), { code: "download_expired" });
  assert.deepEqual(await fs.readdir(f.root), []);
});

test("inconsistent chunk offsets abort the staged artifact", async t => {
  const f = await fixture(t, { badOffset: true });
  await assert.rejects(f.adapter.capture({ file: f.file, timeout: 1 }, f.trigger), { code: "download_changed" });
  assert.deepEqual(await fs.readdir(f.root), []);
  assert.equal(f.artifacts.length, 0);
});

test("existing output is rejected before observation or the browser click", async t => {
  const f = await fixture(t);
  await fs.writeFile(f.file, "existing");
  let clicks = 0;
  await assert.rejects(f.adapter.capture({ file: f.file, timeout: 1 }, async () => { clicks++; }), { code: "file_exists" });
  assert.equal(clicks, 0);
  assert.equal(f.calls.length, 0);
  assert.equal(await fs.readFile(f.file, "utf8"), "existing");
});

test("observer setup failure releases capture and staging before any click", async t => {
  const f = await fixture(t);
  f.adapter.enable = async () => { throw new Error("native observation unavailable"); };
  await assert.rejects(f.adapter.capture({ file: f.file }, () => assert.fail("No click after setup failure")),
    /native observation unavailable/);
  assert.equal(f.adapter.active, null);
  assert.deepEqual(await fs.readdir(f.root), []);
  assert.equal(f.calls.at(-1).method, "Flowork.downloadEnd");
});

test("late trigger errors are preserved without retrying or keeping partial files", async t => {
  const f = await fixture(t);
  const expected = new Error("locator.click: no matching element");
  let triggers = 0;
  await assert.rejects(f.adapter.capture({ file: f.file, timeout: 0.001 }, async () => {
    triggers++;
    await assert.rejects(f.adapter.active.started.promise, { code: "download_timeout" });
    throw expected;
  }), error => error === expected);
  assert.equal(triggers, 1);
  assert.equal(f.adapter.active, null);
  assert.deepEqual(await fs.readdir(f.root), []);
});


test("a click without a native download times out without replay or file transfer", async t => {
  const f = await fixture(t);
  let clicks = 0;
  await assert.rejects(f.adapter.capture({ file: f.file, timeout: 0.001 }, async () => {
    clicks++;
  }), error => {
    assert.equal(error.code, "download_timeout");
    assert.match(error.hint, /cross-origin/);
    assert.match(error.hint, /does not indicate a browser disconnection/);
    return true;
  });
  assert.equal(clicks, 1);
  assert.equal(f.adapter.active, null);
  assert.equal(f.approvals.length, 0);
  assert.equal(f.calls.some(c => c.method === "Flowork.downloadRead"), false);
  assert.equal(f.calls.at(-1).method, "Flowork.downloadEnd");
  assert.deepEqual(await fs.readdir(f.root), []);
  const result = await f.adapter.capture({ file: f.file, timeout: 1 }, f.trigger);
  assert.equal(result.bytes, f.bytes.length);
});
