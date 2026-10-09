"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { BrowserRuntime } = require("./browser-runtime.cjs");
const { BrowserCommandError } = require("./browser-files.cjs");

test("CDP loss is reported separately from an action timeout", async () => {
  const runtime = new BrowserRuntime();
  let connected = true;
  runtime.browser = { isConnected: () => connected };
  const state = { page: { isClosed: () => false, locator: () => ({ click: async () => {
    const error = new Error("Timeout 1000ms exceeded");
    error.name = "TimeoutError";
    throw error;
  } }) }, dialogWaiters: new Set() };
  runtime.states.set("test", state);
  const timedOut = await runtime.execute("browser.click", { tab_id: "test", locator: "button" });
  assert.equal(timedOut.error, "action_timeout");
  assert.equal(timedOut._connection_lost, false);
  connected = false;
  const disconnected = await runtime.execute("browser.snapshot", { tab_id: "test" });
  assert.equal(disconnected.error, "browser_disconnected");
  assert.equal(disconnected._connection_lost, true);
});

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

for (const blockedStage of ["create_tab", "attach_tab", "navigate", "read_tab_info"]) {
  test(`tab-new deadline covers ${blockedStage} and prevents late continuation`, async () => {
    const runtime = new BrowserRuntime();
    let release;
    const stalled = new Promise(resolve => { release = resolve; });
    const calls = [];
    const page = { goto: async () => { calls.push("navigate"); if (blockedStage === "navigate") await stalled; } };
    const state = { id: "tab_created", page };
    runtime.tab = async () => ({ page: { context: () => ({ newPage: async () => {
      calls.push("create_tab"); if (blockedStage === "create_tab") await stalled; return page;
    } }) } });
    runtime.attach = async () => { calls.push("attach_tab"); if (blockedStage === "attach_tab") await stalled; return state; };
    runtime.tabInfo = async () => { calls.push("read_tab_info"); if (blockedStage === "read_tab_info") await stalled; return { tab_id: state.id }; };
    await assert.rejects(runtime.newTab("opener", "https://example.com", 0.02), error => {
      assert.equal(error.code, "tab_new_timeout");
      assert.equal(error.details.stage, blockedStage);
      assert.equal(error.details.tab_id, ["navigate", "read_tab_info"].includes(blockedStage) ? state.id : undefined);
      return true;
    });
    const before = [...calls];
    release();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(calls, before);
  });
}

test("tab-new zero deadline permits slow metadata and returns the created tab", async () => {
  const runtime = new BrowserRuntime();
  const page = { goto: async (_url, options) => assert.equal(options.timeout, 0) };
  runtime.tab = async () => ({ page: { context: () => ({ newPage: async () => page }) } });
  runtime.attach = async () => ({ id: "tab_created", page });
  runtime.tabInfo = async () => { await new Promise(resolve => setTimeout(resolve, 30)); return { tab_id: "tab_created" }; };
  assert.deepEqual(await runtime.newTab("opener", "https://example.com", 0), { tab_id: "tab_created" });
});
