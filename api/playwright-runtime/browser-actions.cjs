"use strict";

// Pure command layer. The owner supplies authenticated browser/tab lookup,
// download transfer and credential consent; no implicit target is selected here.
const { iso } = require("playwright-core/lib/coreBundle");
const crypto = require("node:crypto");
const { BrowserCommandError, regularFile, writeArtifact, defaultArtifact, serializable } = require("./browser-files.cjs");

const TARGET_ACTIONS = new Set(["click", "dblclick", "hover", "fill", "type", "select", "check", "uncheck", "drag", "scroll", "press", "goto", "go-back", "go-forward", "reload", "upload", "drop", "mousemove", "mousedown", "mouseup", "mousewheel", "keydown", "keyup", "dialog-accept", "dialog-dismiss"]);

function omitDefaultExact(source) {
  // Playwright's locator converter round-trips generated code. Its generator
  // omits exact:false, so otherwise-valid explicit defaults fail that check.
  // Normalize only unquoted literal properties, then keep the upstream parser
  // and round-trip validation. Never evaluate Agent locator JavaScript.
  const mask = source.split("");
  for (let i = 0; i < source.length; i++) {
    const delimiter = source[i];
    if (!['"', "'", "`", "/"].includes(delimiter)) continue;
    let characterClass = false;
    mask[i] = "\0";
    for (i++; i < source.length; i++) {
      const char = source[i];
      mask[i] = "\0";
      if (char === "\\") { if (i + 1 < source.length) mask[++i] = "\0"; continue; }
      if (delimiter === "/" && char === "[") characterClass = true;
      if (delimiter === "/" && char === "]") characterClass = false;
      if (char === delimiter && !characterClass) break;
    }
  }
  const text = mask.join("");
  const matches = [...text.matchAll(/([,{])\s*exact\s*:\s*false\s*(?=[,}])/g)];
  for (const match of matches.reverse()) {
    let end = match.index + match[0].length;
    const prefix = match[1] === "{" ? "{" : "";
    if (prefix && text[end] === ",") end++;
    source = source.slice(0, match.index) + prefix + source.slice(end);
  }
  return source;
}

function target(state, args) {
  if (args.ref) {
    const native = state.refs.get(args.ref);
    if (!native) throw new BrowserCommandError("stale_ref", "The element reference is not valid for this tab and observation.", `Run browser snapshot --tab_id ${state.id}.`);
    return state.page.locator(`aria-ref=${native}`);
  }
  if (args.locator) {
    const selector = iso.locatorOrSelectorAsSelector("javascript", args.locator, "data-testid") ||
      iso.locatorOrSelectorAsSelector("javascript", omitDefaultExact(args.locator), "data-testid");
    if (!selector) throw new BrowserCommandError("invalid_locator", "The locator expression is unsupported.", "Use a CSS selector or a supported Playwright locator; put arbitrary JavaScript in run-code.");
    return state.page.locator(selector);
  }
  return null;
}

async function snapshot(state, args = {}, isCurrent = () => true) {
  if (state.dialog) return { dialog: { type: state.dialog.type(), message: state.dialog.message() }, snapshot: null };
  const locator = target(state, args);
  const timeout = (args.timeout ?? 30) * 1000;
  const started = Date.now();
  // AI snapshots do not auto-wait for a missing target in Playwright.
  if (locator) await locator.waitFor({ state: "attached", timeout });
  const remaining = locator && timeout !== 0 ? Math.max(1, timeout - (Date.now() - started)) : timeout;
  const root = locator || state.page;
  const text = await root.ariaSnapshot({ mode: "ai", depth: args.depth, boxes: args.boxes, timeout: remaining });
  const prefix = crypto.randomBytes(5).toString("hex");
  const refs = new Map();
  const result = text.replace(/\[ref=((?:f\d+)?e\d+)\]/g, (_, ref) => {
    const id = `r${prefix}_${ref}`;
    refs.set(id, ref);
    return `[ref=${id}]`;
  });
  const title = await state.page.title();
  if (isCurrent()) state.refs = refs;
  return { url: state.page.url(), title, snapshot: result };
}

async function drop(state, args, locator) {
  await locator.waitFor({ state: "attached", timeout: args.timeout * 1000 });
  return state.downloads.upload(locator, args.file || [], { drop: true, data: args.data || [] });
}

async function evaluate(state, args) {
  const expression = args.file ? (await regularFile(args.file)).toString("utf8") : args.expression;
  // Playwright evaluates string arguments as expressions; an arrow-function
  // string therefore returns a function instead of invoking it. Compile (but
  // NEVER invoke) this wrapper locally, then let Playwright serialize and run
  // it in the target document. Page globals and all Agent code stay in Chrome.
  let evaluateInPage;
  try {
    evaluateInPage = new Function("element", `const value = (\n${expression}\n); return typeof value === 'function' ? value(element) : value;`);
  } catch {
    throw new BrowserCommandError("invalid_script", "Expected a JavaScript expression or function, such as document.title or (el) => el.textContent.");
  }
  let result;
  if (args.ref) {
    const locator = target(state, args);
    if (args.frame_id) {
      const handle = await locator.elementHandle();
      try {
        if (await handle.ownerFrame() !== state.frames.get(args.frame_id))
          throw new BrowserCommandError("frame_mismatch", "The element reference is not in the specified frame.");
      } finally { await handle?.dispose(); }
    }
    result = await locator.evaluate(evaluateInPage);
  } else {
    const frame = args.frame_id ? state.frames.get(args.frame_id) : state.page.mainFrame();
    if (!frame || frame.isDetached()) throw new BrowserCommandError("stale_frame", "The requested frame no longer exists.", "Run frame-list again.");
    result = await frame.evaluate(evaluateInPage);
  }
  result = serializable(result);
  return args.output_file ? await writeArtifact(args.output_file, JSON.stringify(result, null, 2) + "\n") : result;
}

async function executeAction(owner, name, args, isCurrent = () => true) {
  if (name === "tab-list") return { tabs: await owner.listTabs() };
  if (name === "tab-new") return owner.newTab(args.opener_tab_id, args.url, args.timeout ?? 30);
  const state = await owner.tab(args.tab_id);
  const page = state.page;
  const locator = target(state, args);
  const timeout = { timeout: (args.timeout ?? 30) * 1000 };
  let result = {};
  switch (name) {
    case "tab-info": return owner.tabInfo(state);
    case "tab-close": await page.close(); return { closed: true };
    case "snapshot": {
      const observation = await snapshot(state, args);
      return args.output_file ? { ...await writeArtifact(args.output_file, observation.snapshot ?? JSON.stringify(observation)), url: observation.url } : observation;
    }
    case "find": {
      const observation = await snapshot(state, args);
      if (observation.snapshot === null) return observation;
      let matcher;
      try { matcher = args.regex ? new RegExp(args.text) : null; }
      catch { throw new BrowserCommandError("invalid_regex", "The regular expression is invalid."); }
      const lines = observation.snapshot.split("\n");
      const indices = new Set();
      lines.forEach((line, index) => {
        if (matcher ? matcher.test(line) : line.includes(args.text))
          for (let offset = -3; offset <= 3; offset++) if (lines[index + offset] !== undefined) indices.add(index + offset);
      });
      return { url: observation.url, matches: [...indices].sort((a, b) => a - b).map(index => ({ line: index + 1, text: lines[index] })) };
    }
    case "frame-list": {
      state.frames.clear();
      const ids = new Map(page.frames().map(frame => [frame, "frame_" + crypto.randomBytes(6).toString("hex")]));
      for (const [frame, id] of ids) state.frames.set(id, frame);
      return { frames: [...ids].map(([frame, id]) => ({ frame_id: id, parent_id: ids.get(frame.parentFrame()) ?? null, name: frame.name(), url: frame.url() })) };
    }
    case "screenshot": {
      const options = { type: "png", scale: args.hires ? "device" : "css", ...timeout };
      const image = locator ? await locator.screenshot(options) : await page.screenshot({ ...options, fullPage: args.full_page });
      return writeArtifact(args.output_file || await defaultArtifact("png"), image);
    }
    case "goto": await page.goto(args.url, { ...timeout, waitUntil: "domcontentloaded" }); break;
    case "go-back": await page.goBack({ ...timeout, waitUntil: "domcontentloaded" }); break;
    case "go-forward": await page.goForward({ ...timeout, waitUntil: "domcontentloaded" }); break;
    case "reload": await page.reload({ ...timeout, waitUntil: "domcontentloaded" }); break;
    case "click": await locator.click({ ...timeout, button: args.button, modifiers: args.modifiers }); break;
    case "dblclick": await locator.dblclick({ ...timeout, button: args.button }); break;
    case "hover": await locator.hover(timeout); break;
    case "fill":
      await locator.fill(args.text, timeout);
      if (args.submit) await locator.press("Enter", timeout);
      break;
    case "type": await locator.pressSequentially(args.text, timeout); break;
    case "press":
      if (locator) await locator.press(args.key, timeout);
      else await page.keyboard.press(args.key);
      break;
    case "select": {
      try { await locator.selectOption(args.value, timeout); }
      catch (error) {
        // Keep Playwright's wait for asynchronously loaded options. If it expires,
        // report actual values instead of encouraging another guessed selection.
        if (error.name === "TimeoutError") {
          const options = await locator.evaluate(element => element.tagName === "SELECT"
            ? [...element.options].map(option => ({ value: option.value, label: option.label, disabled: option.disabled }))
            : null, undefined, { timeout: 1000 }).catch(() => null);
          if (options && args.value.some(value => !options.some(option => option.value === value)))
            throw new BrowserCommandError("option_not_found", "The requested select value was not available before the action timeout.",
              "Use exact case-sensitive option values, not guessed labels. Current options: " + JSON.stringify(options));
        }
        throw error;
      }
      break;
    }
    case "check": await locator.check(timeout); break;
    case "uncheck": await locator.uncheck(timeout); break;
    case "drag": await target(state, { ref: args.from_ref }).dragTo(target(state, { ref: args.to_ref }), timeout); break;
    case "scroll": {
      const dx = args.direction === "right" ? args.pixels : args.direction === "left" ? -args.pixels : 0;
      const dy = args.direction === "down" ? args.pixels : args.direction === "up" ? -args.pixels : 0;
      if (locator) await locator.hover(timeout);
      await page.mouse.wheel(dx, dy);
      break;
    }
    case "wait-for": await (locator || page.getByText(args.text, { exact: true })).waitFor({ ...timeout, state: args.state }); return { state: args.state };
    case "generate-locator": return { locator: (await locator.normalize()).toString() };
    case "highlight":
      if (args.hide) {
        // The pinned Playwright runtime supports scoped and page-wide removal.
        await owner.hideHighlight(state, args.ref || args.locator);
      } else {
        await locator.highlight();
        state.highlights.set(args.ref || args.locator, locator);
      }
      return { highlighted: !args.hide };
    case "mousemove": await page.mouse.move(args.x, args.y); break;
    case "mousedown": await page.mouse.down({ button: args.button }); state.buttons.add(args.button); break;
    case "mouseup": await page.mouse.up({ button: args.button }); state.buttons.delete(args.button); break;
    case "mousewheel": await page.mouse.wheel(args.delta_x, args.delta_y); break;
    case "keydown": await page.keyboard.down(args.key); state.keys.add(args.key); break;
    case "keyup": await page.keyboard.up(args.key); state.keys.delete(args.key); break;
    case "eval": return evaluate(state, args);
    case "run-code": return owner.runCode(state, args);
    case "cookie-list": return owner.cookies(state, false, args);
    case "cookie-export": return owner.cookies(state, true, args);
    case "download": return owner.download(state, args, locator);
    case "download-status": return state.downloads.status(args.choice_set_id);
    case "download-receive": return state.downloads.receive(args.choice_set_id, args.candidate_id, args.file);
    case "download-cancel": return state.downloads.cancel(args.choice_set_id);
    case "upload": {
      if (locator) {
        await locator.waitFor({ state: "attached", ...timeout });
        result = await state.downloads.upload(locator, args.file);
      }
      else {
        if (!state.chooser) throw new BrowserCommandError("file_chooser_missing", "No file chooser is pending.", "Specify the input with --ref/--locator, or click its upload button first.");
        result = await state.downloads.upload(state.chooser.element(), args.file);
        state.chooser = null;
      }
      break;
    }
    case "drop": result = await drop(state, args, locator); break;
    case "dialog-accept":
    case "dialog-dismiss": {
      if (!state.dialog) throw new BrowserCommandError("dialog_missing", "No JavaScript dialog is pending in this tab.");
      const dialog = state.dialog;
      if (name === "dialog-accept") await dialog.accept(args.text);
      else await dialog.dismiss();
      if (state.dialog === dialog) state.dialog = null;
      break;
    }
    case "console": return owner.console(state, args);
    case "requests": return owner.requests(state, args);
    case "request": return owner.request(state, args);
    default: throw new BrowserCommandError("unsupported_command", "The browser runtime does not recognize this command.");
  }
  if (TARGET_ACTIONS.has(name) && isCurrent() && !page.isClosed()) {
    try { result.observation = await snapshot(state, { timeout: args.timeout }, isCurrent); }
    catch (error) {
      result.observation_error = { stage: "snapshot", code: error.name === "TimeoutError" ? "action_timeout" : (error.code || "observation_failed"), message: String(error.message || error) };
      result.warning = "The action succeeded, but the follow-up snapshot failed. Observe the page before taking the next action; do not repeat the completed action."; }
  }
  return result;
}

module.exports = { executeAction, snapshot, target, TARGET_ACTIONS };
