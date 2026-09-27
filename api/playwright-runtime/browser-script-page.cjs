"use strict";

// Playwright object graph adapters for the native, approval-gated file path.
// No response interception, page download hooks or ungated upload fallback.
const fs = require("node:fs/promises");
const { BrowserCommandError, artifactWriter } = require("./browser-files.cjs");

async function copyArtifact(source, destination) {
  const writer = await artifactWriter(destination);
  try {
    const input = await fs.open(source, "r");
    try {
      for await (const data of input.createReadStream({ autoClose: false })) await writer.write(data);
    } finally { await input.close(); }
    return await writer.commit();
  } catch (error) { await writer.abort(); throw error; }
}

async function scriptPage() {
  // The script can reach other authorized pages through context/pages, popup
  // events or frame.page(). Keep those paths on the same sandbox byte adapter.
  // Arm downloads lazily, on the actual receiver page, before any script action.
  const preparing = new Set();
  this.scriptPreparing = preparing;
  const downloads = new Set([this]);
  this.scriptDownloads = downloads;
  const original = this.state.page;
  const proxies = new WeakMap();
  const originals = new WeakMap();
  const listeners = new WeakMap();
  const isPage = object => typeof object?.frames === "function" && typeof object?.mainFrame === "function";
  const unwrap = value => originals.get(value) || value;
  const wrap = object => {
    if (!object || typeof object !== "object") return object;
    if (Array.isArray(object)) return object.map(wrap);
    if (typeof object.then === "function") return object.then(wrap);
    if (!proxies.has(object)) {
      const proxy = new Proxy(object, handler);
      proxies.set(object, proxy);
      originals.set(proxy, object);
    }
    return proxies.get(object);
  };
  const handler = {
    get: (object, key) => {
      if (key === "waitForEvent") return (event, options = {}) => {
        if (event !== "download" || !isPage(object)) {
          const predicate = typeof options === "function" ? options : options.predicate;
          const wrappedPredicate = predicate && ((...args) => predicate(...args.map(wrap)));
          const waiting = object.waitForEvent(event, typeof options === "function" ? wrappedPredicate
            : wrappedPredicate ? { ...options, predicate: wrappedPredicate } : options);
          return waiting.then(wrap);
        }
        if (typeof options === "function" || options.predicate)
          throw new BrowserCommandError("unsupported_download_predicate", "Download predicates are not supported; wait for the next download from this tab.");
        const ready = (async () => {
          const owner = object === original ? this : await this.downloadsForPage?.(object);
          if (!owner) throw new BrowserCommandError("tab_unavailable", "This script page has no authorized download adapter.");
          downloads.add(owner);
          const file = await owner.newArtifact("download");
          const active = await owner.arm({ file, timeout: options.timeout ?? 30000 });
          await owner.enable();
          return active;
        })();
        preparing.add(ready);
        // Retain rejected preparations until script cleanup; a following
        // mutation must not run after its observer failed to initialize.
        const promise = ready.then(active => active.started.promise).then(wrap);
        promise.catch(() => {});
        return promise;
      };
      const value = object[key];
      if (typeof value !== "function") {
        if (value && ["keyboard", "mouse", "touchscreen"].includes(key)) return wrap(value);
        return value;
      }
      if (key === "setInputFiles" || key === "setFiles") return async (...args) => {
        await Promise.all(preparing);
        // page/frame.setInputFiles(selector, files), locator/element.setInputFiles(files),
        // and chooser.setFiles(files) all refer to sandbox paths, not Chrome's
        // local filesystem. Native uploads also gate FilePayload buffers.
        const selectorForm = key === "setInputFiles" && typeof object.locator === "function" && typeof args[0] === "string" && args.length > 1 &&
          (isPage(object) || typeof object.parentFrame === "function");
        const files = args[selectorForm ? 1 : 0];
        const target = key === "setFiles" ? object.element() : selectorForm ? object.locator(args[0]) : object;
        const options = args[selectorForm ? 2 : 1] || {};
        if (typeof target.waitFor === "function") await target.waitFor({ state: "attached", timeout: options.timeout ?? 30000 });
        const page = isPage(object) ? object : typeof target.page === "function" ? target.page()
          : typeof target.ownerFrame === "function" ? (await target.ownerFrame())?.page() : null;
        const owner = page === original ? this : page && await this.downloadsForPage?.(page);
        if (!owner) throw new BrowserCommandError("tab_unavailable", "The upload target has no authorized file-transfer adapter.");
        await owner.upload(target, files);
      };
      // Playwright locator factories are synchronous. Wrap returned locators
      // separately so their asynchronous actions honor the observer barrier.
      if (/^(locator|frameLocator|getBy|filter$|first$|last$|nth$|and$|or$)/.test(String(key)))
        return (...args) => wrap(value.apply(object, args.map(unwrap)));
      if (["mainFrame", "frame", "frames", "contentFrame", "parentFrame", "ownerFrame", "elementHandle", "elementHandles", "all", "element", "page", "context", "browser", "contexts", "pages", "newPage", "opener", "$", "$$", "waitForSelector"].includes(String(key)))
        return (...args) => wrap(value.apply(object, args.map(unwrap)));
      if (["on", "once", "addListener", "off", "removeListener"].includes(String(key)))
        return (event, listener) => {
          if (!listeners.has(listener)) listeners.set(listener, function (...args) { return listener.apply(wrap(this), args.map(wrap)); });
          return wrap(value.call(object, event, listeners.get(listener)));
        };
      if (/^(click|dblclick|tap|press|type|fill|check|uncheck|setChecked|selectOption|dispatchEvent|evaluate|evaluateHandle|goto|reload|goBack|goForward|dragTo|down|up|move|wheel|insertText)$/.test(String(key)))
        return async (...args) => {
          await Promise.all(preparing);
          const result = value.apply(object, args.map(unwrap));
          return key === "evaluateHandle" ? wrap(result) : result;
        };
      return (...args) => value.apply(object, args.map(unwrap));
    },
  };
  return wrap(original);
}


module.exports = { scriptPage, copyArtifact };
