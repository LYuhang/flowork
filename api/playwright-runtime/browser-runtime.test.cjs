"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { BrowserRuntime } = require("./browser-runtime.cjs");
const { BrowserCommandError } = require("./browser-files.cjs");

test("an action resumed after dialog acceptance cannot invalidate the returned fresh refs", async () => {
  const runtime = new BrowserRuntime();
  let resumeClick;
  let snapshots = 0;
  const dialog = { type: () => "confirm", message: () => "Continue?", accept: async () => {} };
  const state = { id: "t", refs: new Map([["original", "e1"]]), dialogWaiters: new Set(), page: {
    locator: () => ({ click: () => {
      state.dialog = dialog;
      for (const waiter of state.dialogWaiters) waiter(dialog);
      return new Promise(resolve => { resumeClick = resolve; });
    } }),
    isClosed: () => false, url: () => "https://example.com", title: async () => "Example",
    ariaSnapshot: async () => { snapshots++; return '- button "Next" [ref=e2]'; },
  } };
  runtime.tab = async () => state;
  const clicked = await runtime.execute("browser.click", { tab_id: "t", ref: "original" });
  assert.equal(clicked.result.dialog.type, "confirm");
  const accepted = await runtime.execute("browser.dialog-accept", { tab_id: "t" });
  const ref = accepted.result.observation.snapshot.match(/\[ref=(.*?)\]/)[1];
  assert.ok(state.refs.has(ref));
  resumeClick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(snapshots, 1, "The abandoned click must not perform another snapshot");
  assert.ok(state.refs.has(ref), "The ref returned to the Agent remains current");
});

for (const code of ["cookie_consent_required", "cookie_consent_denied", "cookie_format_lossy", "invalid_cookie_format"]) {
  test(`${code} reports no export effect and never writes private files`, async () => {
    const runtime = new BrowserRuntime();
    runtime.tab = async () => ({ id: "test", page: {}, dialogWaiters: new Set(), cdp: {
      send: async method => {
        assert.equal(method, "Flowork.cookieExport");
        return { error: code, message: "Cookie export was rejected.", hint: "Review permission or use JSON format." };
      },
    } });
    runtime.privateFiles = { save: () => assert.fail("A denied export cannot create a file") };
    const result = await runtime.execute("browser.cookie-export", { tab_id: "test", file: "cookies.json", format: "json" });
    assert.equal(result.status, "failed");
    assert.equal(result.error, code);
    assert.equal(result.effects_may_have_occurred, false);
    assert.equal(result.hint, "Review permission or use JSON format.");
    assert.deepEqual(result._artifacts, []);
  });
}

test("Cookie transport failure after dispatch retains unknown outcome", async () => {
  const runtime = new BrowserRuntime();
  runtime.tab = async () => ({ id: "test", page: {}, dialogWaiters: new Set(), cdp: {
    send: async () => { throw new Error("Browser connection closed"); },
  } });
  runtime.privateFiles = { save: () => assert.fail("No export payload was received") };
  const result = await runtime.execute("browser.cookie-export", { tab_id: "test", file: "cookies.json", format: "json" });
  assert.equal(result.status, "unknown");
  assert.equal(result.error, "result_unknown");
  assert.equal(result.effects_may_have_occurred, true);
});

test("script failures still conservatively report possible earlier effects", async () => {
  const runtime = new BrowserRuntime();
  runtime.tab = async () => ({ page: {}, dialogWaiters: new Set() });
  runtime.runCode = async () => { throw new BrowserCommandError("unserializable_result", "An earlier page write may have succeeded."); };
  const result = await runtime.execute("browser.run-code", { tab_id: "test" });
  assert.equal(result.effects_may_have_occurred, true);
});

test("direct ambiguous click gives locator recovery guidance without retrying", async () => {
  const runtime = new BrowserRuntime();
  let clicks = 0;
  runtime.tab = async () => ({ page: { locator: () => ({ click: async () => {
    clicks++;
    throw new Error("locator.click: Error: strict mode violation: locator('#save') resolved to 2 elements");
  } }) }, dialogWaiters: new Set() });
  const result = await runtime.execute("browser.click", { tab_id: "test", locator: "#save" });
  assert.equal(result.status, "failed");
  assert.equal(result.error, "locator_ambiguous");
  assert.match(result.hint, /fresh refs/);
  assert.match(result.hint, /do not choose the first match blindly/);
  assert.equal(clicks, 1);
});

test("script strict errors keep possible earlier effects rather than suggesting an atomic rejection", async () => {
  const runtime = new BrowserRuntime();
  runtime.tab = async () => ({ page: {}, dialogWaiters: new Set() });
  runtime.runCode = async () => { throw new Error("locator.click: Error: strict mode violation: resolved to 2 elements"); };
  const result = await runtime.execute("browser.run-code", { tab_id: "test" });
  assert.equal(result.error, "browser_command_failed");
  assert.equal(result.effects_may_have_occurred, true);
});

test("typed download failures retain their code while raw disconnects remain unknown", async () => {
  const runtime = new BrowserRuntime();
  runtime.tab = async () => ({ page: {}, dialogWaiters: new Set() });
  runtime.runCode = async () => { throw new BrowserCommandError("popup_download_unavailable", "A newly opened tab closed before capture.", "Inspect before retrying."); };
  const failed = await runtime.execute("browser.run-code", { tab_id: "test" });
  assert.equal(failed.status, "failed");
  assert.equal(failed.error, "popup_download_unavailable");
  assert.equal(failed.effects_may_have_occurred, true);
  assert.equal(failed.hint, "Inspect before retrying.");
  runtime.runCode = async () => { throw new Error("Target page, context or browser has been closed"); };
  const unknown = await runtime.execute("browser.run-code", { tab_id: "test" });
  assert.equal(unknown.status, "unknown");
  assert.equal(unknown.error, "result_unknown");
  assert.equal(unknown.effects_may_have_occurred, true);
  assert.match(unknown.hint, /restore control in the side panel/);
  assert.match(unknown.hint, /Do not repeat the command automatically/);
  assert.match(unknown.hint, /rediscover tabs/);
});

test("read-only transport interruptions explain restoration without claiming a write", async () => {
  const runtime = new BrowserRuntime();
  runtime.tab = async () => { throw new Error("Target page, context or browser has been closed"); };
  const result = await runtime.execute("browser.snapshot", { tab_id: "test" });
  assert.equal(result.status, "failed");
  assert.equal(result.effects_may_have_occurred, false);
  assert.match(result.hint, /restore control in the side panel/);
  assert.match(result.hint, /Do not repeat the command automatically/);
});

test("highlight removal uses scoped and all-frame lifecycle APIs without rebuilding overlays", async () => {
  const runtime = new BrowserRuntime();
  const calls = [];
  const first = { hideHighlight: async () => calls.push("first") };
  const second = { hideHighlight: async () => calls.push("second") };
  const state = { page: { hideHighlight: async () => calls.push("all-frames") },
    highlights: new Map([["#first", first], ["#second", second]]) };
  await runtime.hideHighlight(state, "#first");
  assert.deepEqual(calls, ["first"]);
  assert.deepEqual([...state.highlights.keys()], ["#second"]);
  await runtime.hideHighlight(state, "#missing");
  assert.deepEqual(calls, ["first"]);
  await runtime.hideHighlight(state);
  assert.deepEqual(calls, ["first", "all-frames"]);
  assert.equal(state.highlights.size, 0);
});

test("failed highlight teardown preserves tracking instead of reporting false cleanup", async () => {
  const runtime = new BrowserRuntime();
  const failure = new Error("Browser disconnected");
  const state = { page: { hideHighlight: async () => { throw failure; } },
    highlights: new Map([["#first", { hideHighlight: async () => { throw failure; } }]]) };
  await assert.rejects(runtime.hideHighlight(state, "#first"), failure);
  assert.equal(state.highlights.size, 1);
  await assert.rejects(runtime.hideHighlight(state), failure);
  assert.equal(state.highlights.size, 1);
});
