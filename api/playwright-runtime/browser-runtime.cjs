#!/usr/bin/env node
"use strict";

// Long-lived per-turn worker INSIDE the Agent sandbox. It does not start Chrome,
// expose an MCP server, infer a current tab or hold platform user credentials.
const { chromium } = require("playwright-core");
const readline = require("node:readline");
const crypto = require("node:crypto");
const { executeAction, TARGET_ACTIONS } = require("./browser-actions.cjs");
const { BrowserCommandError, regularFile, writeArtifact, serializable } = require("./browser-files.cjs");
const { NativeBrowserDownloads } = require("./browser-native-downloads.cjs");
const { PrivateBrowserFiles } = require("./browser-private-files.cjs");

const READS = new Set(["tab-list", "tab-info", "snapshot", "find", "frame-list", "screenshot", "generate-locator", "cookie-list", "console", "requests", "request", "wait-for", "download-status"]);
const BUFFER_ENTRIES = 1000;

function safeText(value) {
  return String(value).replace(/\b(Bearer\s+)[A-Za-z0-9._~+\/-]+/gi, "$1[redacted]")
    .replace(/((?:authorization|cookie|set-cookie|password|api[_-]?key)\s*[:=]\s*)[^\r\n]+/gi, "$1[redacted]");
}

function safeHeaders(headers) {
  return Object.fromEntries(Object.entries(headers).map(([key, value]) => [key,
    /^(authorization|proxy-authorization|cookie|set-cookie|x-api-key)$/i.test(key) ? "[redacted]" : safeText(value)]));
}

class BrowserRuntime {
  constructor(progress = () => {}, requestTransfer = async () => { throw new BrowserCommandError("approval_unavailable", "File-transfer approval is unavailable."); }) {
    this.progress = progress;
    this.requestTransfer = requestTransfer;
    this.browser = null;
    this.states = new Map();
    this.pendingPages = new Map();
    this.epoch = crypto.randomBytes(6).toString("hex");
    this.closed = false;
    this.contextListeners = [];
    this.commandArtifacts = new Map();
  }

  async initialize({ endpoint, bearer, private_root }) {
    if (this.browser) throw new BrowserCommandError("already_initialized", "This browser runtime is already initialized.");
    this.privateFiles = new PrivateBrowserFiles(private_root);
    this.browser = await chromium.connectOverCDP(endpoint, { headers: { Authorization: `Bearer ${bearer}` }, timeout: 20000 });
    for (const context of this.browser.contexts()) {
      const listener = page => { this.attach(page).catch(() => {}); };
      context.on("page", listener);
      this.contextListeners.push([context, listener]);
      await Promise.all(context.pages().map(page => this.attach(page)));
    }
    return { status: "succeeded", message: "Connected to the authorized browser extension." };
  }

  async attach(page) {
    if (this.pendingPages.has(page)) return this.pendingPages.get(page);
    const pending = this.attachPage(page);
    this.pendingPages.set(page, pending);
    try { return await pending; }
    catch (error) { this.pendingPages.delete(page); throw error; }
  }

  async attachPage(page) {
    const cdp = await page.context().newCDPSession(page);
    const metadata = await cdp.send("Flowork.tabInfo");
    if (!metadata.target_id) throw new BrowserCommandError("invalid_target", "The extension did not identify an authorized tab.");
    const id = `tab_${metadata.target_id}`;
    const state = { id, page, cdp, metadata, refs: new Map(), frames: new Map(), highlights: new Map(),
      keys: new Set(), buttons: new Set(), dialog: null, chooser: null, dialogWaiters: new Set(),
      logs: [], network: [], sequence: 0, droppedLogs: 0, droppedRequests: 0, };
    state.downloads = new NativeBrowserDownloads(state, event => this.progress(event), {
      requestTransfer: input => this.requestTransfer(input),
      recordArtifact: artifact => this.commandArtifacts.set(artifact.file, artifact),
      downloadsForPage: async page => (await this.attach(page)).downloads,
    });
    this.states.set(id, state);
    cdp.on("Flowork.cookieConsentRevoked", event => {
      if (typeof event.grant_id === "string") void this.privateFiles?.revoke(event.grant_id).catch(() => {});
    });
    page.on("dialog", dialog => {
      state.dialog = dialog;
      for (const resolve of state.dialogWaiters) resolve(dialog);
    });
    page.on("filechooser", chooser => { state.chooser = chooser; });
    page.on("framenavigated", () => { state.refs.clear(); state.frames.clear(); state.highlights.clear(); });
    page.on("close", () => { this.states.delete(id); this.pendingPages.delete(page); });
    const log = (level, text) => {
      state.logs.push({ sequence: ++state.sequence, level, text: safeText(text) });
      if (state.logs.length > BUFFER_ENTRIES) { state.logs.shift(); state.droppedLogs++; }
    };
    page.on("console", message => log(message.type(), message.text()));
    page.on("pageerror", error => log("error", error.message));
    page.on("request", request => {
      state.network.push({ sequence: ++state.sequence, request_id: `req_${this.epoch}_${crypto.randomBytes(5).toString("hex")}`, request });
      if (state.network.length > BUFFER_ENTRIES) { state.network.shift(); state.droppedRequests++; }
    });
    return state;
  }

  async tab(id) {
    if (!this.browser?.isConnected() || this.closed) throw new BrowserCommandError("browser_disconnected", "The authorized browser is disconnected.", "Use flowork-cli browser tab-list to request access in the open authorized side panel, then snapshot the intended returned tab. Do not replay earlier actions; if access still fails, report the diagnostic instead of looping.");
    const state = this.states.get(id);
    if (!state || state.page.isClosed()) throw new BrowserCommandError("tab_unavailable", "This tab is closed, stale or not authorized for this Chat.", "Run tab-list; do not guess another browser or tab ID.");
    return state;
  }

  async tabInfo(state) {
    state.metadata = await state.cdp.send("Flowork.tabInfo");
    return { tab_id: state.id, window_id: state.metadata.window_id, active: state.metadata.active,
      title: state.metadata.title, url: state.metadata.url, connected: true };
  }

  async listTabs() {
    // The connection only advertises authorized targets; no global Chrome query.
    for (const context of this.browser.contexts())
      await Promise.all(context.pages().map(page => this.attach(page)));
    return Promise.all([...this.states.values()].filter(state => !state.page.isClosed()).map(state => this.tabInfo(state)));
  }

  async newTab(openerId, url, timeout = 30) {
    let stage = "resolve_opener", state, expired = false, timer;
    const deadline = timeout > 0 ? Date.now() + timeout * 1000 : null;
    const timeoutError = () => {
      const error = new BrowserCommandError("tab_new_timeout",
        `Creating a tab exceeded ${timeout}s during ${stage}.`,
        "The runtime will be released. On the next explicit command, list tabs and inspect any created tab before deciding what to do. Do not repeat tab-new automatically.");
      error.details = { stage, ...(state ? { tab_id: state.id } : {}), effects_may_have_occurred: stage !== "resolve_opener" };
      return error;
    };
    const checkpoint = next => {
      if (expired || (deadline !== null && Date.now() >= deadline)) throw timeoutError();
      stage = next;
    };
    const run = async () => {
      const opener = await this.tab(openerId);
      checkpoint("create_tab");
      const page = await opener.page.context().newPage();
      checkpoint("attach_tab");
      state = await this.attach(page);
      checkpoint("navigate");
      let navigationError;
      try {
        if (url !== "about:blank") await page.goto(url, {
          waitUntil: "domcontentloaded", timeout: deadline === null ? 0 : Math.max(1, deadline - Date.now()),
        });
      } catch (error) {
        if (deadline !== null && error.name === "TimeoutError") throw timeoutError();
        navigationError = error;
      }
      checkpoint("read_tab_info");
      const info = await this.tabInfo(state);
      checkpoint("complete");
      return navigationError ? { ...info,
        warning: "The tab was created, but navigation failed. Inspect it before retrying.",
        navigation_error: safeText(navigationError.message) } : info;
    };
    if (deadline === null) return run();
    try {
      const limit = new Promise((_, reject) => {
        timer = setTimeout(() => { expired = true; reject(timeoutError()); }, Math.max(0, deadline - Date.now()));
      });
      return await Promise.race([run(), limit]);
    } finally { clearTimeout(timer); }
  }

  cursor(state, sequence) { return `${this.epoch}:${state.id}:${sequence}`; }

  after(state, cursor) {
    if (!cursor) return 0;
    const prefix = `${this.epoch}:${state.id}:`;
    if (!cursor.startsWith(prefix) || !/^\d+$/.test(cursor.slice(prefix.length)))
      throw new BrowserCommandError("stale_cursor", "This cursor belongs to a different tab or browser runtime.", "Omit --after to read the current buffer.");
    return Number(cursor.slice(prefix.length));
  }

  console(state, args) {
    const ranks = { error: 0, warning: 1, warn: 1, info: 2, log: 2, debug: 3 };
    const entries = state.logs.filter(item => item.sequence > this.after(state, args.after) && (ranks[item.level] ?? 2) <= ranks[args.level]).slice(0, args.limit);
    return { messages: entries.map(({ sequence, ...item }) => item),
      next_cursor: this.cursor(state, entries.at(-1)?.sequence ?? this.after(state, args.after)),
      dropped_entries: state.droppedLogs, history: "Captured since this runtime connected; bounded to 1000 entries." };
  }

  requests(state, args) {
    const entries = state.network.filter(item => item.sequence > this.after(state, args.after) && (!args.url_contains || item.request.url().includes(args.url_contains))).slice(0, args.limit);
    return { requests: entries.map(item => ({ request_id: item.request_id, method: item.request.method(), url: safeText(item.request.url()), resource_type: item.request.resourceType(), failure: item.request.failure() })),
      next_cursor: this.cursor(state, entries.at(-1)?.sequence ?? this.after(state, args.after)),
      dropped_entries: state.droppedRequests, history: "Captured since this runtime connected; bounded to 1000 entries." };
  }

  async request(state, args) {
    const entry = state.network.find(item => item.request_id === args.request_id);
    if (!entry) throw new BrowserCommandError("request_unavailable", "The request was not captured by this tab/runtime or was evicted.", "Use request IDs returned by requests.");
    const response = await entry.request.response();
    const result = { request_id: entry.request_id, url: safeText(entry.request.url()), method: entry.request.method(),
      request_headers: safeHeaders(await entry.request.allHeaders()), status: response?.status() ?? null,
      response_headers: response ? safeHeaders(await response.allHeaders()) : null, failure: entry.request.failure() };
    if (args.output_file) {
      if (!response) throw new BrowserCommandError("response_unavailable", "The response body is unavailable; no file was saved.", "Inspect requests and the page state; do not resend the HTTP request merely to recover its body.");
      let body;
      try { body = await response.body(); }
      catch { throw new BrowserCommandError("response_unavailable", "The response body could not be retrieved; no file was saved.", "Inspect requests and the page state; do not resend the HTTP request merely to recover its body."); }
      result.artifact = await writeArtifact(args.output_file, body);
    }
    return result;
  }

  async hideHighlight(state, key) {
    // Use Playwright's lifecycle APIs, not removal of its overlay DOM. The
    // injected highlighter retains selectors/listeners and can recreate its
    // overlay; page.hideHighlight also reaches same/cross-origin frames.
    if (key) {
      await state.highlights.get(key)?.hideHighlight();
      state.highlights.delete(key);
    } else {
      await state.page.hideHighlight();
      state.highlights.clear();
    }
  }

  async runCode(state, args) {
    const code = args.file ? (await regularFile(args.file)).toString("utf8") : args.code;
    // The process itself is confined by the Chat sandbox. This is deliberately
    // not advertised as a Node vm security sandbox. Browser authority is fenced
    // on the trusted transport/extension side even if code accesses page.context.
    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
    let run;
    try { run = new AsyncFunction("page", "require", "console", `return await (${code})(page);`); }
    catch { throw new BrowserCommandError("invalid_script", "Expected JavaScript in the form async (page) => { ... }.", "Use browser run-code --help for the script contract."); }
    const scriptConsole = Object.fromEntries(["log", "info", "warn", "error", "debug"].map(level => [level,
      (...values) => this.progress({ status: "running", message: values.map(value => safeText(typeof value === "string" ? value : JSON.stringify(value))).join(" ") }),
    ]));
    try {
      const page = await state.downloads.scriptPage();
      const value = serializable(await run(page, require, scriptConsole));
      return args.output_file ? writeArtifact(args.output_file, JSON.stringify(value, null, 2) + "\n") : value;
    } finally { await state.downloads.close(); }
  }

  async cookies(state, exporting, args) {
    // The dedicated bridge methods (not global Storage.getCookies) own scope
    // and site consent. No cookie values may enter ordinary command stdout.
    const result = await state.cdp.send(exporting ? "Flowork.cookieExport" : "Flowork.cookieMetadata", { format: args.format });
    if (result.error) throw new BrowserCommandError(result.error, result.message, result.hint);
    if (!exporting) return result;
    const file = await this.privateFiles.save(args.file, result.content, result.grant_id, state.id);
    try {
      const permission = await state.cdp.send("Flowork.cookieStatus");
      if (!permission.allowed || permission.grant_id !== result.grant_id)
        throw new BrowserCommandError("cookie_consent_revoked", "Cookie permission changed during export; the private file was removed.", "Stop using the private Cookie file. Ask the user to review site-specific consent before any new export; ordinary page actions need no Cookie export.");
    } catch (error) { await this.privateFiles.revoke(result.grant_id); throw error; }
    return { ...file, cookie_count: result.cookie_count, url: result.url };
  }

  async download(state, args, locator) {
    // Implemented by a byte-transfer adapter, not native remote Chrome paths.
    return state.downloads.capture(args, async active => {
      if (!args.url) return locator.click({ timeout: args.timeout * 1000 });
      try { return await state.cdp.send("Flowork.downloadStartUrl", { capture_id: active.capture_id, url: args.url }); }
      catch (error) {
        throw new BrowserCommandError("download_url_start_failed", error.message,
          "Inspect Chrome Downloads before any further attempt; a download may already exist. URL mode needs extension 0.3.4 or newer. Report the browser error; do not automatically replay the download or fetch the URL from the sandbox.");
      }
    });
  }

  async execute(operation, args) {
    const name = operation.replace(/^browser\./, "");
    this.commandArtifacts = new Map();
    let resolveDialog;
    let state;
    let dispatched = false;
    let observationCurrent = true;
    try {
      if (args.tab_id) state = await this.tab(args.tab_id);
      if (state?.dialog && !["dialog-accept", "dialog-dismiss", "snapshot", "tab-info", "tab-close"].includes(name))
        throw new BrowserCommandError("dialog_pending", "A JavaScript dialog is blocking this tab.", "Use dialog-accept or dialog-dismiss before another page action.");
      const dialog = new Promise(resolve => { resolveDialog = resolve; });
      if (state && TARGET_ACTIONS.has(name) && !name.startsWith("dialog-")) state.dialogWaiters.add(resolveDialog);
      dispatched = true;
      const action = executeAction(this, name, args, () => observationCurrent);
      // A click opening an alert may wait until that alert closes. Let the next
      // CLI command resolve it instead of deadlocking the serialized queue.
      const result = state && state.dialogWaiters.has(resolveDialog)
        ? await Promise.race([action, dialog.then(value => {
          // The original action may resume after a later dialog-accept command.
          // Its abandoned snapshot must not replace that command's fresh refs.
          observationCurrent = false;
          return { dialog: { type: value.type(), message: safeText(value.message()) }, hint: "Resolve the dialog with dialog-accept or dialog-dismiss, then observe the resulting state. Do not repeat the original trigger; it may resume after the dialog closes." };
        })])
        : await action;
      if (result?.observation_error) result.observation_error.message = safeText(result.observation_error.message);
      if (name !== "cookie-export") {
        for (const artifact of [result, result?.artifact]) {
          if (artifact && typeof artifact.file === "string" && Number.isInteger(artifact.bytes) && /^[a-f0-9]{64}$/.test(artifact.sha256))
            this.commandArtifacts.set(artifact.file, artifact);
        }
      }
      return { status: "succeeded", ...(args.tab_id ? { tab_id: args.tab_id } : {}), result,
        _connection_lost: !this.browser?.isConnected(),
        _artifacts: [...this.commandArtifacts.values()],
        message: "Browser command completed." };
    } catch (error) {
      // Give direct locator commands an actionable code. Do not reinterpret a
      // run-code/eval error: scripts can fail after arbitrary earlier effects.
      if (!(error instanceof BrowserCommandError) && TARGET_ACTIONS.has(name) &&
        (args.locator || args.ref) && /\bstrict mode violation:/.test(String(error.message))) {
        error = new BrowserCommandError("locator_ambiguous", error.message,
          "The locator matched multiple elements. Run snapshot for fresh refs or narrow --locator to one intended element. Inspect the page before retrying; do not choose the first match blindly.");
      }
      // Consent refusals precede reading values; format refusals precede sending
      // serialized values to the sandbox. Neither can create an export file.
      // Keep transport failures and post-write failures conservative.
      if (name === "cookie-export" && error instanceof BrowserCommandError &&
        ["cookie_consent_required", "cookie_consent_denied", "cookie_format_lossy", "invalid_cookie_format"].includes(error.code)) dispatched = false;
      // Typed adapter errors already describe the known outcome (for example,
      // a popup capture missed and no file committed). Do not erase that code
      // just because its message mentions "closed" or "connection".
      const interrupted = !(error instanceof BrowserCommandError) && /closed|disconnect|connection|crash/i.test(String(error.message));
      const unknown = interrupted && dispatched && !READS.has(name);
      return { status: unknown ? "unknown" : "failed", error: unknown ? "result_unknown" : error.code || (error.name === "TimeoutError" ? "action_timeout" : "browser_command_failed"),
        _connection_lost: !this.browser?.isConnected(),
        message: safeText(error.message),
        ...(error.details ? { details: error.details } : {}),
        _artifacts: [...this.commandArtifacts.values()],
        hint: error.hint || (interrupted
          ? "Browser access was interrupted. A new explicit tab-list can reconnect in the authorized window while the side panel is open. Do not repeat the previous action automatically. Inspect existing tabs and earlier effects; ask the user only if the window or permission is unavailable."
          : dispatched && !READS.has(name) ? "Run snapshot on the intended tab and inspect output files; earlier effects may already have occurred. Do not replay the action or entire script automatically." : "Read this command's --help and inspect the current tab with snapshot or tab-info. For action_timeout on a slow page, use a suitable --timeout for the next observation; a timeout alone does not prove disconnection."),
        effects_may_have_occurred: dispatched && !READS.has(name) };
    } finally { observationCurrent = false; state?.dialogWaiters.delete(resolveDialog); }
  }

  async close() {
    this.closed = true;
    await this.privateFiles?.revoke();
    for (const [context, listener] of this.contextListeners) context.off("page", listener);
    for (const state of this.states.values()) {
      await state.downloads.close({ turnEnd: true }).catch(() => {});
      for (const key of state.keys) await state.page.keyboard.up(key).catch(() => {});
      for (const button of state.buttons) await state.page.mouse.up({ button }).catch(() => {});
      await this.hideHighlight(state).catch(() => {});
    }
    // On a CDP connection close disconnects the Playwright client; the extension
    // refuses Browser.close and owns detaching debugger targets itself.
    await this.browser?.close().catch(() => {});
    this.states.clear();
  }
}

async function main() {
  if (process.argv.includes("--version")) {
    process.stdout.write(`flowork-browser-runtime ${require("./package.json").version} (playwright-core ${require("playwright-core/package.json").version})\n`);
    return;
  }
  let activeId;
  const write = payload => process.stdout.write(JSON.stringify(payload) + "\n");
  const transfers = new Map();
  const runtime = new BrowserRuntime(progress => write({ id: activeId, progress }), input => {
    const request_id = crypto.randomUUID();
    return new Promise((resolve, reject) => {
      transfers.set(request_id, { resolve, reject });
      write({ id: activeId, transfer: { request_id, input } });
    });
  });
  let chain = Promise.resolve();
  const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  input.on("line", line => {
    let incoming;
    try { incoming = JSON.parse(line); }
    catch { write({ id: activeId, result: { status: "failed", error: "invalid_runtime_frame", message: "Invalid browser runtime control frame." } }); return; }
    // Approval replies bypass the serialized command queue: that command is
    // currently suspended awaiting this very reply, not a second shell job.
    if (incoming.transfer_reply) {
      const pending = transfers.get(incoming.transfer_reply);
      if (pending) { transfers.delete(incoming.transfer_reply); pending.resolve(incoming.result); }
      return;
    }
    chain = chain.then(async () => {
      const command = incoming;
      activeId = command.id;
      const result = command.operation === "initialize" ? await runtime.initialize(command.arguments) : await runtime.execute(command.operation, command.arguments);
      write({ id: activeId, result });
    }).catch(error => write({ id: activeId, result: { status: "failed", error: "browser_runtime_failed", message: safeText(error.message) } }));
  });
  let closing = false;
  const close = async () => {
    if (closing) return;
    closing = true;
    for (const pending of transfers.values()) pending.reject(new BrowserCommandError("approval_cancelled", "The file-transfer command ended."));
    transfers.clear();
    const timer = setTimeout(() => process.exit(1), 2000);
    timer.unref();
    await runtime.close();
    process.exit(0);
  };
  input.on("close", close);
  process.on("SIGTERM", close);
  process.on("SIGINT", close);
}

module.exports = { BrowserRuntime, safeText, safeHeaders };
if (require.main === module) main().catch(() => process.exit(1));
