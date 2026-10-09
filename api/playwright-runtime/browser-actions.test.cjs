"use strict";

// Unit evidence only. Live acceptance is exclusively through side-panel Agent
// conversations, not direct calls from this test harness into a real browser.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { writeArtifact, regularFile, filePayloads, serializable } = require("./browser-files.cjs");
const { target, executeAction, snapshot } = require("./browser-actions.cjs");

test("binary artifacts preserve bytes/hash and never overwrite existing files or symlinks", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "browser-file-unit-"));
  try {
    const file = path.join(directory, "result.bin");
    const bytes = Buffer.from([0, 255, 128, 13, 10]);
    const result = await writeArtifact(file, bytes);
    assert.equal(result.bytes, bytes.length);
    assert.match(result.sha256, /^[a-f0-9]{64}$/);
    assert.deepEqual(await regularFile(file), bytes);
    assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
    await assert.rejects(writeArtifact(file, "changed"), error => error.code === "file_exists");
    const link = path.join(directory, "link");
    await fs.symlink(path.join(directory, "missing"), link);
    await assert.rejects(writeArtifact(link, "changed"), error => error.code === "file_exists");
    assert.deepEqual(await regularFile(file), bytes);
    assert.deepEqual((await fs.readdir(directory)).sort(), ["link", "result.bin"]);
    const payload = await filePayloads([file]);
    assert.deepEqual(payload, [{ name: "result.bin", mimeType: "application/octet-stream", buffer: bytes }]);
    await assert.rejects(regularFile(directory), error => error.code === "invalid_file");
  } finally { await fs.rm(directory, { recursive: true, force: true }); }
});

test("script serialization does not silently lose nested values", () => {
  assert.equal(serializable(undefined), null);
  assert.deepEqual(serializable({ empty: null, count: 1 }), { empty: null, count: 1 });
  for (const value of [{ v: undefined }, { v: NaN }, { v: Infinity }, { v: 1n }, new Map(), { v: () => 1 }])
    assert.throws(() => serializable(value), error => error.code === "unserializable_result");
  const cycle = {}; cycle.value = cycle;
  assert.throws(() => serializable(cycle), error => error.code === "unserializable_result");
});

test("refs are scoped to the exact observation, never inferred or reused", async () => {
  let selector;
  const state = { id: "tab_test", refs: new Map(), page: {
    ariaSnapshot: async () => '- button "Submit" [ref=e1]',
    url: () => "https://example.com", title: async () => "Example",
    locator: value => { selector = value; return { selector: value }; },
  } };
  const first = await snapshot(state);
  const ref = first.snapshot.match(/\[ref=(.*?)\]/)[1];
  assert.notEqual(ref, "e1");
  target(state, { ref });
  assert.equal(selector, "aria-ref=e1");
  await snapshot(state);
  assert.throws(() => target(state, { ref }), error => {
    assert.equal(error.code, "stale_ref");
    assert.match(error.hint, /flowork-cli browser snapshot --tab-id tab_test/);
    assert.doesNotMatch(error.hint, /--tab_id/);
    return true;
  });
  assert.throws(() => target(state, { ref: "e1" }), error => error.code === "stale_ref");
});

test("a superseded in-flight snapshot cannot replace newer refs", async () => {
  let current = true;
  const newer = new Map([["newer", "e2"]]);
  const state = { refs: newer, page: {
    ariaSnapshot: async () => { current = false; return '- button "Old" [ref=e1]'; },
    url: () => "https://example.com", title: async () => "Example",
  } };
  await snapshot(state, {}, () => current);
  assert.equal(state.refs, newer);
});

test("supported locator expressions are parsed instead of executed", () => {
  const state = { page: { locator: selector => selector } };
  assert.match(target(state, { locator: "getByRole('button', {name:'Submit', exact:true})" }), /internal:role/);
  assert.equal(target(state, { locator: "#submit" }), "#submit");
  assert.throws(() => target(state, { locator: "process.exit(99)" }), error => error.code === "invalid_locator");
});

test("explicit exact:false matches Playwright defaults without rewriting quoted text or executing code", () => {
  const state = { page: { locator: selector => selector } };
  for (const [explicit, implicit] of [
    ["getByText('Ready', {exact:false})", "getByText('Ready')"],
    ["getByRole('button', {name:'Ready', exact: false})", "getByRole('button', {name:'Ready'})"],
    ["getByRole('button', {exact:false, name:'Ready'})", "getByRole('button', {name:'Ready'})"],
    ["getByText('{exact:false}', {exact:false})", "getByText('{exact:false}')"],
    ['getByText("quote\\\" {exact:false}", {exact:false})', 'getByText("quote\\\" {exact:false}")'],
    ["getByText(/[,{}]exact:false/i, {exact:false})", "getByText(/[,{}]exact:false/i)"],
    ["getByText('Ready', {exact:false}).getByRole('button', {name:'Go', exact:false})", "getByText('Ready').getByRole('button', {name:'Go'})"],
  ]) assert.equal(target(state, { locator: explicit }), target(state, { locator: implicit }), explicit);
  assert.throws(() => target(state, { locator: "getByText('Ready', {exact:false}); process.exit(99)" }), error => error.code === "invalid_locator");
  assert.throws(() => target(state, { locator: "getByText('Ready', {exact:process.exit(99)})" }), error => error.code === "invalid_locator");
});

test("a failed post-action snapshot does not turn a completed click into a failed action", async () => {
  let clicks = 0;
  const state = { id: "t", refs: new Map([["r1", "e1"]]), page: {
    locator: () => ({ click: async () => { clicks++; } }),
    ariaSnapshot: async () => { throw new Error("lost observation"); },
    isClosed: () => false,
  } };
  const result = await executeAction({ tab: async () => state }, "click", { tab_id: "t", ref: "r1", button: "left" });
  assert.equal(clicks, 1);
  assert.match(result.warning, /action succeeded/);
});

test("select timeout reports real case-sensitive values without retrying the mutation", async () => {
  let selections = 0;
  const timeout = Object.assign(new Error("Timed out"), { name: "TimeoutError" });
  let options = [{ value: "Advanced", label: "Advanced mode", disabled: false }];
  const state = { refs: new Map([["r", "e1"]]), page: {
    locator: () => ({
      selectOption: async () => { selections++; throw timeout; },
      evaluate: async () => options,
    }),
  } };
  const run = value => executeAction({ tab: async () => state }, "select", { tab_id: "t", ref: "r", value: [value], timeout: 0.01 });
  await assert.rejects(run("advanced"), error => error.code === "option_not_found" && error.hint.includes('"value":"Advanced"'));
  assert.equal(selections, 1);
  await assert.rejects(run("Advanced"), error => error === timeout, "An existing but disabled/covered option is not a missing value");
  options = null;
  await assert.rejects(run("advanced"), error => error === timeout, "Observation failure preserves the original timeout");
});

test("eval invokes source functions in the page realm with the selected element", async () => {
  const runInPage = async (fn, element) => {
    assert.equal(typeof fn, "function");
    return vm.runInNewContext(`(${fn})(element)`, { element, document: { title: "Page realm" } });
  };
  const frame = { isDetached: () => false, evaluate: fn => runInPage(fn) };
  const state = { refs: new Map([["ref_heading", "e2"]]), frames: new Map([["frame_child", frame]]), page: {
    mainFrame: () => frame,
    locator: () => ({ evaluate: fn => runInPage(fn, { textContent: "Heading" }) }),
  } };
  const owner = { tab: async () => state };
  for (const expression of ["document.title", "() => document.title", "async () => document.title", "Promise.resolve(document.title)"])
    assert.equal(await executeAction(owner, "eval", { expression }), "Page realm");
  assert.equal(await executeAction(owner, "eval", { ref: "ref_heading", expression: "(el) => el.textContent" }), "Heading");
  assert.equal(await executeAction(owner, "eval", { frame_id: "frame_child", expression: "() => document.title" }), "Page realm");
  assert.equal(await executeAction(owner, "eval", { expression: "typeof process" }), "undefined", "Source must not execute in the Node realm");
  await assert.rejects(executeAction(owner, "eval", { expression: "(" }), { code: "invalid_script" });
});

test("no implicit tab lookup for listing or creation; new tab always supplies its opener", async () => {
  const calls = [];
  const owner = {
    listTabs: async () => [{ tab_id: "t" }],
    newTab: async (...args) => { calls.push(args); return { tab_id: "new" }; },
    tab: () => { throw new Error("Unexpected implicit target"); },
  };
  assert.deepEqual(await executeAction(owner, "tab-list", {}), { tabs: [{ tab_id: "t" }] });
  assert.deepEqual(await executeAction(owner, "tab-new", { opener_tab_id: "t", url: "about:blank" }), { tab_id: "new" });
  assert.deepEqual(calls, [["t", "about:blank", 30]]);
});

test("observation timeouts apply to reads and post-action snapshots, including zero", async () => {
  for (const seconds of [undefined, 0, 60]) {
    const seen = [];
    const state = { refs: new Map(), page: {
      ariaSnapshot: async options => { seen.push(['snapshot', options.timeout]); return '- heading "Ready"'; },
      goto: async (_url, options) => seen.push(['goto', options.timeout]),
      url: () => 'https://example.com', title: async () => 'Ready', isClosed: () => false,
    } };
    const owner = { tab: async () => state };
    for (const name of ['snapshot', 'find', 'goto']) {
      seen.length = 0;
      await executeAction(owner, name, { tab_id: 't', url: 'https://example.com', text: 'Ready', timeout: seconds });
      const expected = (seconds ?? 30) * 1000;
      assert.deepEqual(seen, name === 'goto' ? [['goto', expected], ['snapshot', expected]] : [['snapshot', expected]]);
    }
  }
});

test("post-action timeout reports the observation stage without repeating navigation", async () => {
  let navigations = 0;
  const state = { refs: new Map(), page: {
    goto: async () => { navigations++; }, isClosed: () => false,
    ariaSnapshot: async () => { throw Object.assign(new Error('Snapshot timed out after 60000ms'), {name: 'TimeoutError'}); },
  } };
  const result = await executeAction({ tab: async () => state }, 'goto', {tab_id:'t', url:'https://example.com', timeout:60});
  assert.equal(navigations, 1);
  assert.deepEqual(result.observation_error, {stage:'snapshot', code:'action_timeout', message:'Snapshot timed out after 60000ms'});
  assert.match(result.warning, /do not repeat/);
});

test("target snapshots wait for attachment and use the remaining observation budget", async () => {
  const calls = [];
  const locator = {
    waitFor: async options => { calls.push(['wait', options]); },
    ariaSnapshot: async options => { calls.push(['snapshot', options.timeout]); return '- text: Ready'; },
  };
  const state = { refs: new Map(), page: { locator: () => locator, url: () => 'https://example.com', title: async () => 'Ready' } };
  await snapshot(state, {locator:'#late', timeout:15});
  assert.deepEqual(calls[0], ['wait', {state:'attached', timeout:15000}]);
  assert.ok(calls[1][1] > 0 && calls[1][1] <= 15000);
  locator.waitFor = async () => { throw Object.assign(new Error('Target did not appear'), {name:'TimeoutError'}); };
  await assert.rejects(snapshot(state, {locator:'#late', timeout:1}), {name:'TimeoutError'});
  assert.equal(calls.length, 2, 'No snapshot or retry after the target wait times out');
});
