"use strict";

// Native Chrome owns the download. Only approved, registered local files cross
// into the sandbox; HTTP responses are never intercepted or replayed here.
const path = require("node:path");
const { BrowserCommandError, artifactWriter, defaultArtifact, approvedTransferFiles } = require("./browser-files.cjs");
const { scriptPage, copyArtifact } = require("./browser-script-page.cjs");

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  promise.catch(() => {});
  return { promise, resolve, reject };
}
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
function publicDownloadInfo(info) {
  // The host needs Chrome's numeric ID for the authorization fence. Agent
  // commands use the opaque platform tab_id from tab-list, never this number.
  const { tab_id, ...metadata } = info;
  return metadata;
}

class NativeBrowserDownloads {
  constructor(state, progress = () => {}, { requestTransfer, recordArtifact = () => {}, newArtifact = defaultArtifact, downloadsForPage } = {}) {
    Object.assign(this, { state, progress, requestTransfer, recordArtifact, newArtifact, downloadsForPage });
    this.active = null;
    this.closed = false;
  }

  async arm({ file, timeout = 30000 }) {
    if (this.closed) throw new BrowserCommandError("download_expired", "The browser turn ended.");
    if (this.active || this.arming) throw new BrowserCommandError("download_pending", "This tab already has a pending download.", "Resolve or cancel the existing choice_set_id; do not trigger another download.");
    this.arming = true;
    let writer, begin;
    try {
      writer = await artifactWriter(file);
      begin = await this.state.cdp.send("Flowork.downloadBegin", {});
      if (begin.error) throw new BrowserCommandError(begin.error, begin.message, begin.hint);
      if (!begin.capture_id) throw new BrowserCommandError("download_unavailable", "Chrome did not start download observation. No click was sent.");
      if (this.closed) throw new BrowserCommandError("download_expired", "The browser turn ended before the download trigger.");
      return this.active = { capture_id: begin.capture_id, file, writer, started: deferred(), timeout,
        began: Date.now(), observedStart: false, info: null, observing: null, saved: null };
    } catch (error) {
      await writer?.abort();
      if (begin?.capture_id) await this.state.cdp.send("Flowork.downloadEnd", { capture_id: begin.capture_id }).catch(() => {});
      throw error;
    } finally { this.arming = false; }
  }

  async enable() {
    const active = this.active;
    if (!active || active.observing) return;
    active.observing = this.observe(active).then(info => {
      active.info = info;
      if (info.status === "selection_required") {
        const error = new BrowserCommandError("download_selection_required", "Multiple files were downloaded locally. No bytes were transferred.",
          "Call render_choices with choice_set_id, then download-receive for the confirmed candidate. Selection does not approve transfer.");
        error.details = info;
        active.started.reject(error);
        return;
      }
      active.started.resolve(this.downloadObject(active));
    }).catch(error => active.started.reject(error));
  }

  async observe(active) {
    let stable = "", settledAt = 0;
    while (this.active === active && !this.closed) {
      let info;
      try { info = publicDownloadInfo(await this.state.cdp.send("Flowork.downloadInfo", { capture_id: active.capture_id })); }
      catch (error) {
        if (String(error.message).includes("download_local_cancelled:"))
          throw new BrowserCommandError("download_local_cancelled",
            "The user cancelled local file confirmation. No file was transferred; the local download was kept.",
            "Do not repeat the download or transfer unless the user explicitly asks.");
        throw error;
      }
      if (info.status !== "watching") active.observedStart = true;
      if (info.status === "local_confirmation_required" && !active.localConfirmationReported) {
        active.localConfirmationReported = true;
        this.progress({ status: "waiting_for_local_confirmation", message: "Waiting for the user to confirm the downloaded file in the extension side panel. No file metadata or bytes have been shared. Do not repeat the download trigger." });
      }
      if (info.status === "ready" || info.status === "selection_required") {
        // Collect a short burst of completed downloads rather than choosing the
        // first callback while sibling download notifications are still queued.
        const key = JSON.stringify(info.candidates);
        if (key !== stable) { stable = key; settledAt = Date.now(); }
        else if (Date.now() - settledAt >= 350) return info;
      } else { stable = ""; settledAt = 0; }
      if (!active.observedStart && active.timeout > 0 && Date.now() - active.began >= active.timeout)
        throw new BrowserCommandError("download_timeout", "No attributable Chrome download started before the wait timeout.", "Inspect the current page and Chrome Downloads; the click may have navigated or opened media instead. A cross-origin <a download> may not trigger a download. Use an actual download control or attachment response. This timeout alone does not indicate a browser disconnection. Do not repeat the trigger automatically.");
      await pause(150);
    }
    throw new BrowserCommandError("download_cancelled", "The download observation ended.");
  }

  downloadObject(active) {
    const candidate = active.info.candidates[0];
    const save = async destination => {
      if (active.saved) {
        if (path.resolve(destination) === active.saved.file) return active.saved;
        const copy = await copyArtifact(active.saved.file, destination);
        this.recordArtifact(copy);
        return copy;
      }
      return this.receive(active.info.choice_set_id, candidate.candidate_id, destination);
    };
    return { suggestedFilename: () => candidate.name, url: () => candidate.source, page: () => this.state.page,
      failure: async () => null, saveAs: save,
      path: async () => (await save(active.file)).file,
      createReadStream: async () => require("node:fs").createReadStream((await save(active.file)).file),
      cancel: async () => this.cancel(active.info.choice_set_id) };
  }

  async capture(args, trigger) {
    const active = await this.arm({ file: args.file, timeout: (args.timeout ?? 30) * 1000 });
    try {
      await this.enable();
      await trigger();
      const download = await active.started.promise;
      return await download.saveAs(args.file);
    } catch (error) {
      if (error.code === "download_selection_required") {
        await active.writer.abort(); active.writer = null;
        return { ...active.info, message: error.message, hint: error.hint };
      }
      await this.end(active);
      throw error;
    }
  }

  async status(setId) {
    const active = this.requireCapture(setId);
    const info = publicDownloadInfo(await this.state.cdp.send("Flowork.downloadInfo", { capture_id: active.capture_id }));
    active.info = info;
    return info;
  }

  requireCapture(setId) {
    if (!this.active || this.active.info?.choice_set_id !== setId)
      throw new BrowserCommandError("download_expired", "The choice_set_id is not pending in this tab and Agent turn.", "Do not guess a candidate or repeat the download automatically.");
    return this.active;
  }

  async receive(setId, candidateId, file) {
    const active = this.requireCapture(setId);
    const candidate = active.info.candidates.find(item => item.candidate_id === candidateId);
    if (!candidate) throw new BrowserCommandError("invalid_candidate", "Use a registered candidate returned by this capture.");
    let preserve = false;
    try {
      if (!active.writer || path.resolve(file) !== path.resolve(active.file)) {
        await active.writer?.abort();
        active.writer = await artifactWriter(file);
      }
      const decision = await this.requestTransfer({ choice_set_id: setId, candidate_id: candidateId });
      if (decision.status === "selection_required") {
        preserve = true;
        await active.writer.abort(); active.writer = null;
        const error = new BrowserCommandError("download_selection_required", decision.message,
          "Call render_choices with choice_set_id, then download-receive for the confirmed candidate. Do not repeat the trigger.");
        error.details = active.info;
        throw error;
      }
      if (decision.status !== "approved") throw new BrowserCommandError("approval_" + decision.status,
        decision.message || "File transfer was not approved. No bytes were read.", "Stop this transfer and report the decision. Do not resubmit a denied, cancelled or expired approval automatically; a new transfer needs explicit user intent.");
      let offset = 0, lastProgress = 0;
      for (;;) {
        const chunk = await this.state.cdp.send("Flowork.downloadRead", {
          capture_id: active.capture_id, transfer_id: decision.transfer_id, offset });
        const bytes = Buffer.from(chunk.data, "base64");
        if (chunk.offset !== offset + bytes.length || chunk.offset > candidate.bytes)
          throw new BrowserCommandError("download_changed", "The approved download returned inconsistent file bytes.");
        await active.writer.write(bytes);
        offset = chunk.offset;
        if (Date.now() - lastProgress >= 1000) {
          this.progress({ status: "transferring", bytes_received: offset, bytes_total: candidate.bytes,
            message: "Transferring the approved local download to the sandbox." });
          lastProgress = Date.now();
        }
        if (chunk.eof) break;
      }
      if (offset !== candidate.bytes) throw new BrowserCommandError("download_truncated", "The approved file ended before its registered byte count.");
      active.saved = await active.writer.commit(); active.writer = null;
      this.recordArtifact(active.saved);
      return { ...active.saved, suggested_filename: candidate.name, message: "Approved file bytes saved in the sandbox; durable storage acknowledgement is pending." };
    } finally { if (!preserve) await this.end(active); }
  }

  async cancel(setId) {
    await this.end(this.requireCapture(setId));
    return { status: "cancelled", message: "Download capture released. The local file was kept; no new download was triggered." };
  }

  async end(active) {
    if (this.active === active) this.active = null;
    active.started.reject(new BrowserCommandError("download_cancelled", "The download capture ended."));
    await active.writer?.abort(); active.writer = null;
    await this.state.cdp.send("Flowork.downloadEnd", { capture_id: active.capture_id }).catch(() => {});
  }

  async scriptPage() { return scriptPage.call(this); }

  async upload(target, files, options = {}) {
    return approvedTransferFiles(target, files, { ...options, tabId: this.state.id, requestTransfer: this.requestTransfer });
  }

  async close({ turnEnd = false } = {}) {
    if (turnEnd) this.closed = true;
    if (this.scriptPreparing) await Promise.allSettled(this.scriptPreparing);
    this.scriptPreparing = null;
    if (this.scriptDownloads) await Promise.all([...this.scriptDownloads].filter(owner => owner !== this).map(owner => owner.close({ turnEnd })));
    this.scriptDownloads = null;
    if (this.active && (turnEnd || this.active.info?.status !== "selection_required")) await this.end(this.active);
    else if (this.active?.writer) { await this.active.writer.abort(); this.active.writer = null; }
  }
}

module.exports = { NativeBrowserDownloads };
