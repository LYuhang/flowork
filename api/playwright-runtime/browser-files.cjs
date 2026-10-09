"use strict";

// Files are read beside the Agent, never interpreted as paths on the user's PC.
const fs = require("node:fs/promises");
const { constants } = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const os = require("node:os");

class BrowserCommandError extends Error {
  constructor(code, message, hint = "") {
    super(message);
    this.code = code;
    this.hint = hint;
  }
}

async function regularFile(filename) {
  const handle = await fs.open(filename, constants.O_RDONLY | constants.O_NONBLOCK);
  try {
    if (!(await handle.stat()).isFile())
      throw new BrowserCommandError("invalid_file", "The input must be a regular sandbox file.");
    return await handle.readFile();
  } finally {
    await handle.close();
  }
}

async function writeArtifact(filename, content) {
  const data = Buffer.isBuffer(content) ? content : Buffer.from(content);
  const destination = path.resolve(filename);
  const parent = await fs.realpath(path.dirname(destination));
  const staged = path.join(parent, ".browser-" + crypto.randomUUID());
  let handle;
  try {
    handle = await fs.open(staged, "wx", 0o600);
    await handle.writeFile(data);
    await handle.sync();
    await handle.close();
    handle = undefined;
    // Atomic and no-clobber, including dangling symlinks at the destination.
    await fs.link(staged, destination);
  } catch (error) {
    if (error.code === "EEXIST")
      throw new BrowserCommandError("file_exists", "The output file already exists; it was not replaced.", "Inspect the existing file and choose a new output path only if needed. Do not repeat an earlier page action merely to save its result.");
    throw error;
  } finally {
    await handle?.close();
    await fs.unlink(staged).catch(() => {});
  }
  return { file: destination, bytes: data.length, sha256: crypto.createHash("sha256").update(data).digest("hex") };
}

async function artifactWriter(filename) {
  const destination = path.resolve(filename);
  const parent = await fs.realpath(path.dirname(destination));
  // Refuse an existing destination before triggering a browser download. Link
  // at commit still handles a concurrent creator without overwriting it.
  try {
    await fs.lstat(destination);
    throw new BrowserCommandError("file_exists", "The output file already exists; it was not replaced.", "Inspect the existing file and choose a new output path only if needed. Do not repeat an earlier page action merely to save its result.");
  } catch (error) { if (error.code !== "ENOENT") throw error; }
  const staged = path.join(parent, ".browser-" + crypto.randomUUID());
  const handle = await fs.open(staged, "wx", 0o600);
  const hash = crypto.createHash("sha256");
  let bytes = 0;
  let closed = false;
  return {
    async write(data) {
      if (closed) throw new Error("Artifact writer is closed");
      await handle.writeFile(data);
      hash.update(data); bytes += data.length;
    },
    async commit() {
      if (closed) throw new Error("Artifact writer is closed");
      try {
        await handle.sync();
        await handle.close(); closed = true;
        await fs.link(staged, destination);
        return { file: destination, bytes, sha256: hash.digest("hex") };
      } catch (error) {
        if (error.code === "EEXIST") throw new BrowserCommandError("file_exists", "The output destination was created by another operation; it was not replaced.");
        throw error;
      } finally {
        if (!closed) { await handle.close(); closed = true; }
        await fs.unlink(staged).catch(() => {});
      }
    },
    async abort() {
      if (!closed) { await handle.close(); closed = true; }
      await fs.unlink(staged).catch(() => {});
    },
  };
}

async function transferFiles(target, filenames, { drop = false, data = [] } = {}) {
  // Streaming avoids Playwright FilePayload's fixed aggregate-size ceiling.
  // The browser constructs real File objects; no sandbox path crosses into
  // native Chrome's local-file API. Handles remain in the target's own frame.
  const state = await target.evaluateHandle(element => {
    if (!element || element.nodeType !== 1) throw new Error("File transfer requires an element");
    return { element, transfer: new DataTransfer(), parts: [] };
  });
  const result = [];
  try {
    for (const filename of filenames) {
      const prepared = typeof filename !== "string";
      const name = prepared ? filename.name : path.basename(filename);
      const handle = prepared ? filename.handle : await fs.open(filename, constants.O_RDONLY | constants.O_NONBLOCK);
      let bytes = 0;
      const hash = crypto.createHash("sha256");
      try {
        if (!(await handle.stat()).isFile()) throw new BrowserCommandError("invalid_file", "Upload sources must be regular sandbox files.");
        const buffer = Buffer.alloc(192 * 1024);
        for (;;) {
          const read = await handle.read(buffer, 0, buffer.length, bytes);
          if (!read.bytesRead) break;
          const chunk = buffer.subarray(0, read.bytesRead);
          hash.update(chunk); bytes += chunk.length;
          await state.evaluate((state, encoded) => {
            state.parts.push(Uint8Array.from(atob(encoded), char => char.charCodeAt(0)));
          }, chunk.toString("base64"));
        }
        const mime = { ".txt": "text/plain", ".csv": "text/csv", ".json": "application/json", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".pdf": "application/pdf", ".html": "text/html", ".zip": "application/zip" };
        await state.evaluate((state, file) => {
          state.transfer.items.add(new File(state.parts, file.name, { type: file.type }));
          state.parts = [];
        }, { name, type: (prepared && filename.mimeType) || mime[path.extname(name).toLowerCase()] || "application/octet-stream" });
        result.push({ name, bytes, sha256: hash.digest("hex") });
      } finally { if (!prepared) await handle.close(); }
    }
    await state.evaluate((state, options) => {
      if (options.drop) {
        for (const entry of options.data) {
          const separator = entry.indexOf("=");
          state.transfer.setData(entry.slice(0, separator), entry.slice(separator + 1));
        }
        for (const type of ["dragenter", "dragover", "drop"])
          state.element.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: state.transfer }));
      } else {
        if (state.element.tagName !== "INPUT" || state.element.type !== "file") throw new Error("The target is not a file input");
        if (!state.element.multiple && state.transfer.files.length > 1) throw new Error("The target file input does not accept multiple files");
        if (state.element.webkitdirectory) throw new Error("Directory upload is not supported by this file command");
        state.element.files = state.transfer.files;
        state.element.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
        state.element.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }, { drop, data });
    return { files: result };
  } finally { await state.dispose(); }
}

async function approvedTransferFiles(target, input, { requestTransfer, tabId, ...options }) {
  const files = typeof input === "string" ? [input] : Array.isArray(input) ? input : [input];
  // Clearing an input or dragging only text sends no files and needs no gate.
  if (!files.length) return transferFiles(target, [], options);
  if (typeof requestTransfer !== "function")
    throw new BrowserCommandError("approval_unavailable", "File-transfer approval is unavailable. No file bytes were sent.");
  const prepared = [];
  let element;
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "flowork-upload-"));
  try {
    // Pin the concrete element/document, not a locator that could resolve to a
    // different frame or page while the user is reading the approval card.
    element = typeof target.elementHandle === "function" ? await target.elementHandle() : target;
    if (!element) throw new BrowserCommandError("target_missing", "The file target no longer exists.");
    const documentUrl = await element.evaluate(el => el.ownerDocument.URL);
    const ownerFrame = typeof element.ownerFrame === "function" ? await element.ownerFrame() : null;
    const pageUrl = ownerFrame?.page().url();
    // Inherited documents such as about:srcdoc otherwise hide the receiving
    // website from the approval card. Keep both frame and top-level identity.
    const destination = pageUrl && pageUrl !== documentUrl
      ? `${documentUrl} (within ${pageUrl})` : documentUrl;
    for (const file of files) {
      const pathname = path.join(directory, crypto.randomUUID());
      const handle = await fs.open(pathname, "wx+", 0o600);
      // The snapshot has no pathname to replace or edit during the human wait.
      await fs.unlink(pathname);
      const entry = { handle, name: typeof file === "string" ? path.basename(file) : file?.name, mimeType: file?.mimeType, bytes: 0 };
      prepared.push(entry);
      const hash = crypto.createHash("sha256");
      const write = async bytes => { await handle.writeFile(bytes); hash.update(bytes); entry.bytes += bytes.length; };
      if (typeof file === "string") {
        const source = await fs.open(file, constants.O_RDONLY | constants.O_NONBLOCK);
        try {
          if (!(await source.stat()).isFile()) throw new BrowserCommandError("invalid_file", "Upload sources must be regular sandbox files.");
          for await (const chunk of source.createReadStream({ autoClose: false })) await write(chunk);
        } finally { await source.close(); }
      } else {
        if (!entry.name || typeof entry.name !== "string" || !Buffer.isBuffer(file?.buffer))
          throw new BrowserCommandError("invalid_file", "Use a sandbox path or FilePayload with name, mimeType and a Buffer.");
        await write(Buffer.from(file.buffer));
      }
      entry.sha256 = hash.digest("hex");
    }
    const decision = await requestTransfer({ direction: "upload", tab_id: tabId, destination,
      files: prepared.map(({ name, bytes, sha256 }) => ({ name, bytes, sha256 })) });
    if (decision.status !== "approved") throw new BrowserCommandError("approval_" + decision.status,
      decision.message || "File transfer was not approved. No file bytes were sent.", "Stop this transfer and report the decision. Do not resubmit a denied, cancelled or expired approval automatically; a new transfer needs explicit user intent.");
    const unchanged = await element.evaluate((el, url) => el.isConnected && el.ownerDocument.URL === url, documentUrl)
      && (!pageUrl || ownerFrame.page().url() === pageUrl);
    if (!unchanged) throw new BrowserCommandError("upload_target_changed", "The approved upload target changed. No file bytes were sent.", "Inspect the page and request a new upload approval.");
    return await transferFiles(element, prepared, options);
  } finally {
    await Promise.all(prepared.map(file => file.handle.close()));
    await fs.rmdir(directory);
    if (element && element !== target) await element.dispose();
  }
}

async function defaultArtifact(extension) {
  const directory = "/data/browser-media";
  await fs.mkdir(directory, { recursive: true });
  return path.join(directory, `${crypto.randomUUID()}.${extension}`);
}

async function filePayloads(filenames) {
  // Always use FilePayload bytes, not setInputFiles('/data/...') against a
  // remote browser. Remote Chrome does not have the sandbox filesystem.
  const mime = { ".txt": "text/plain", ".csv": "text/csv", ".json": "application/json",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".pdf": "application/pdf", ".html": "text/html", ".zip": "application/zip" };
  return Promise.all(filenames.map(async filename => ({
    name: path.basename(filename),
    mimeType: mime[path.extname(filename).toLowerCase()] || "application/octet-stream",
    buffer: await regularFile(filename),
  })));
}

function serializable(value) {
  // JSON.stringify silently erases undefined, functions and non-finite values.
  // Report these explicitly rather than describing lost data as success.
  const seen = new Set();
  const visit = item => {
    if (item === null || typeof item === "string" || typeof item === "boolean") return;
    if (typeof item === "number" && Number.isFinite(item)) return;
    if (typeof item !== "object") throw new BrowserCommandError("unserializable_result", "Return JSON-compatible data, not undefined, functions, BigInt or non-finite numbers.");
    if (seen.has(item)) throw new BrowserCommandError("unserializable_result", "The script returned a cyclic object.");
    if (!Array.isArray(item) && Object.getPrototypeOf(item) !== Object.prototype)
      throw new BrowserCommandError("unserializable_result", "Return plain objects or arrays, not handles or class instances.");
    seen.add(item);
    Object.values(item).forEach(visit);
    seen.delete(item);
  };
  // A script with no return is a valid action; represent it explicitly as null.
  if (value === undefined) return null;
  visit(value);
  return value;
}

module.exports = { BrowserCommandError, regularFile, writeArtifact, artifactWriter, defaultArtifact, filePayloads, transferFiles, approvedTransferFiles, serializable };
