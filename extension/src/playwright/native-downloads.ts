/** Observe Chrome downloads or explicitly start one URL download; never replay a site action.
 * Local paths stay in this module. Callers receive opaque capture IDs and may
 * read bytes only after the host has approved the exact download record.
 */
export type DownloadRecord = Pick<chrome.downloads.DownloadItem,
  "id" | "url" | "finalUrl" | "filename" | "fileSize" | "state" | "exists" | "startTime" | "danger">;
type RequestRecord = { requestId: string; tabId: number; url: string };
type Capture = {
  id: string; tabId: number; started: number; tabs: Set<number>;
  urlMode: boolean; urlRequested?: boolean; directDownloadId?: number;
  requests: Map<string, { tabId: number; urls: Set<string> }>;
  downloads: Map<number, DownloadRecord>; selected?: DownloadRecord;
  error?: string; reader?: ReadableStreamDefaultReader<Uint8Array>;
  abort: AbortController; offset: number; remainder?: Uint8Array;
  candidates: Map<string, DownloadRecord>; readAllowed: boolean; reading: boolean;
  local: Map<string, DownloadRecord>; confirmed: Map<number, DownloadRecord>;
};
export type DownloadChrome = Pick<typeof chrome, "downloads" | "tabs" | "webRequest" | "extension" | "permissions">;

export function localFileUrl(filename: string): string {
  // Encode path components, not the complete URL. '#' and '%' are legal in
  // downloaded names and must never become a fragment or percent escape.
  const normalized = filename.replace(/\\/g, "/");
  if (normalized.startsWith("//")) throw new Error("Network-share downloads are not supported");
  if (!normalized.startsWith("/") && !/^[A-Za-z]:\//.test(normalized))
    throw new Error("The browser did not return an absolute local file path");
  return "file://" + (normalized.startsWith("/") ? "" : "/") + normalized.split("/")
    .map((part, index) => index === 0 && /^[A-Za-z]:$/.test(part) ? part : encodeURIComponent(part)).join("/");
}

export class NativeDownloads {
  private capture: Capture | undefined;
  private closed = false;
  constructor(private readonly api: DownloadChrome, private readonly windowId: number,
    private readonly ownsTab: (tabId: number) => boolean,
    private readonly readFile: typeof fetch = (...args) => globalThis.fetch(...args),
    private readonly changed: () => void = () => {}) {}

  private readonly onRequest = (request: RequestRecord) => {
    const capture = this.capture;
    if (!capture) return;
    // Keep foreign request identities locally too: equal URLs in unrelated
    // tabs must make attribution ambiguous, not leak that tab's file.
    if (capture.requests.size >= 10000 && !capture.requests.has(request.requestId)) {
      capture.error = "download_observation_overflow";
      this.changed();
      return;
    }
    const record = capture.requests.get(request.requestId) || { tabId: request.tabId, urls: new Set<string>() };
    record.urls.add(request.url);
    capture.requests.set(request.requestId, record);
  };
  private readonly onCreated = (item: chrome.downloads.DownloadItem) => {
    const capture = this.capture;
    if (capture && Date.parse(item.startTime) >= capture.started) capture.downloads.set(item.id, item);
  };
  private readonly onTab = (tab: chrome.tabs.Tab) => {
    const capture = this.capture;
    if (capture && tab.id !== undefined && tab.windowId === this.windowId
      && tab.openerTabId !== undefined && capture.tabs.has(tab.openerTabId)) capture.tabs.add(tab.id);
  };
  private readonly onDetached = (tabId: number) => {
    if (this.capture?.tabs.has(tabId)) {
      this.capture.error = "download_window_changed";
      this.changed();
    }
  };

  async begin(tabId: number, urlMode = false): Promise<Record<string, unknown>> {
    if (this.closed) throw new Error("The browser download session has ended");
    if (this.capture) throw new Error("A browser download is already pending; finish or cancel it first");
    if (!await this.api.extension.isAllowedFileSchemeAccess())
      return { error: "file_access_required", message: "Enable Allow access to file URLs in the Flowork extension details, then retry. No download was triggered." };
    if (!await this.api.permissions.contains({ permissions: ["downloads", "webRequest"], origins: ["http://*/*", "https://*/*", "file:///*"] }))
      return { error: "download_permissions_required", message: "The extension needs download observation and local file access permissions. No download was triggered." };
    const tab = await this.api.tabs.get(tabId);
    if (this.closed || this.capture || !this.ownsTab(tabId) || tab.windowId !== this.windowId)
      throw new Error("The download target is no longer authorized");
    this.capture = { urlMode, id: crypto.randomUUID(), tabId, started: Date.now(), tabs: new Set([tabId]),
      requests: new Map(), downloads: new Map(), candidates: new Map(), readAllowed: false, reading: false,
      local: new Map(), confirmed: new Map(),
      abort: new AbortController(), offset: 0 };
    try {
      this.api.webRequest.onBeforeRequest.addListener(this.onRequest, { urls: ["http://*/*", "https://*/*"] });
      this.api.downloads.onCreated.addListener(this.onCreated);
      this.api.tabs.onCreated.addListener(this.onTab);
      this.api.tabs.onDetached.addListener(this.onDetached);
    } catch (error) {
      await this.clear();
      throw error;
    }
    return { capture_id: this.capture.id, status: "watching" };
  }

  async startUrl(tabId: number, id: string, source: string): Promise<Record<string, unknown>> {
    let url: URL;
    try { url = new URL(source); } catch { throw new Error("download_url_invalid: Supply an absolute HTTP(S) resource URL"); }
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password)
      throw new Error("download_url_unsupported: Use an HTTP(S) resource URL without embedded credentials. Blob URLs and media manifests need a separate workflow.");
    const capture = this.current(tabId, id);
    if (!capture.urlMode) throw new Error("download_mode_mismatch: Start a new URL download capture");
    if (capture.urlRequested) throw new Error("download_already_started: Inspect the existing download; do not start it again");
    const tab = await this.api.tabs.get(tabId);
    this.current(tabId, id);
    if (tab.windowId !== this.windowId) throw new Error("download_window_changed");
    // Recheck after the await: concurrent CDP requests must not start twice.
    if (capture.urlRequested) throw new Error("download_already_started: Inspect the existing download");
    capture.urlRequested = true;
    // Chrome supplies host cookies. Do not export credentials, spoof headers,
    // navigate the page, or infer identity from another download's URL.
    const downloadId = await this.api.downloads.download({ url: url.href, saveAs: false, conflictAction: "uniquify" });
    this.current(tabId, id);
    if (!Number.isSafeInteger(downloadId)) throw new Error("download_start_failed: Chrome returned no download ID");
    capture.directDownloadId = downloadId;
    return { status: "started", capture_id: id };
  }

  private current(tabId: number, id: string): Capture {
    const capture = this.capture;
    if (this.closed || !capture || capture.id !== id || capture.tabId !== tabId || !this.ownsTab(tabId))
      throw new Error("The browser download capture is no longer active or authorized");
    if (capture.error) throw new Error(capture.error);
    if (capture.selected && !this.isOwnedDownload(capture, capture.selected))
      throw new Error("download_source_changed: The approved download is no longer attributable to this tab");
    return capture;
  }

  private isOwnedDownload(capture: Capture, item: DownloadRecord): boolean {
    if (capture.urlMode) return item.id === capture.directDownloadId;
    const confirmed = capture.confirmed.get(item.id);
    if (confirmed) return confirmed.filename === item.filename && confirmed.fileSize === item.fileSize
      && confirmed.url === item.url && confirmed.finalUrl === item.finalUrl;
    const matching = [...capture.requests.values()].filter(request => request.urls.has(item.url) || request.urls.has(item.finalUrl));
    const owned = matching.some(request => capture.tabs.has(request.tabId));
    if (owned && matching.some(request => !capture.tabs.has(request.tabId)))
      throw new Error("download_ambiguous: Owned and unrelated requests match the download. No further file bytes will be transferred.");
    return owned;
  }

  /** Extension shell only. These names must not reach CDP or the embedded app. */
  localConfirmations(): { capture_id: string; files: { id: string; name: string; bytes: number }[] } | null {
    const capture = this.capture;
    if (!capture || capture.error || !this.ownsTab(capture.tabId) || !capture.local.size) return null;
    return { capture_id: capture.id, files: [...capture.local].map(([id, item]) => ({
      id, name: item.filename.replace(/\\/g, "/").split("/").pop() || "Download", bytes: item.fileSize,
    })) };
  }

  async confirmLocal(id: string, fileId: string | null): Promise<void> {
    const active = this.capture;
    if (!active) throw new Error("This download confirmation has expired");
    const capture = this.current(active.tabId, id);
    if (!capture.local.size) throw new Error("This download confirmation has expired");
    if (fileId === null) {
      capture.error = "download_local_cancelled: The user cancelled local file confirmation. The local file was kept.";
      capture.local.clear(); this.changed(); return;
    }
    const selected = capture.local.get(fileId);
    if (!selected) throw new Error("Unknown local download candidate");
    const tab = await this.api.tabs.get(capture.tabId);
    const [fresh] = await this.api.downloads.search({ id: selected.id });
    this.current(capture.tabId, id);
    if (tab.windowId !== this.windowId || !fresh || !fresh.exists || fresh.state !== "complete"
      || !["safe", "accepted", "allowlistedByPolicy"].includes(fresh.danger)
      || fresh.filename !== selected.filename || fresh.fileSize !== selected.fileSize
      || fresh.url !== selected.url || fresh.finalUrl !== selected.finalUrl)
      throw new Error("The local download changed. Cancel and inspect browser Downloads.");
    capture.confirmed.set(selected.id, { ...selected });
    // onCreated commonly has an empty filename and fileSize=-1. Replace that
    // early record with the exact completed record the user just confirmed.
    capture.downloads.set(selected.id, { ...fresh });
    // The explicit choice excludes other unproven files from this capture.
    for (const item of capture.downloads.values()) if (!this.isOwnedDownload(capture, item)) capture.downloads.delete(item.id);
    capture.local.clear(); this.changed();
  }

  async info(tabId: number, id: string): Promise<Record<string, unknown>> {
    const capture = this.current(tabId, id);
    const tab = await this.api.tabs.get(tabId);
    if (tab.windowId !== this.windowId) throw new Error("download_window_changed");
    this.current(tabId, id);
    if (capture.urlMode) {
      if (capture.directDownloadId === undefined) return { status: "watching", capture_id: id };
      const [item] = await this.api.downloads.search({ id: capture.directDownloadId });
      this.current(tabId, id);
      if (!item) throw new Error("download_unavailable: The URL download is no longer in Chrome Downloads");
      if (item.state === "interrupted")
        throw new Error(`download_interrupted: ${item.error || "Chrome interrupted the URL download"}. Inspect Chrome Downloads; do not restart automatically.`);
      if (!["safe", "accepted", "allowlistedByPolicy"].includes(item.danger))
        throw new Error("download_blocked: Review Chrome Downloads; the file is not cleared for transfer");
      capture.downloads.set(item.id, item);
    }
    // Chrome has no tab identity for Blob/data downloads. Never infer ownership
    // from URLs/timestamps; ask the human locally before exposing metadata.
    let localDownloading = false;
    if (!capture.urlMode && !capture.confirmed.size) for (const candidate of capture.downloads.values()) {
      if (!/^(blob:|data:)/.test(candidate.url) || this.isOwnedDownload(capture, candidate)) continue;
      const [item] = await this.api.downloads.search({ id: candidate.id });
      this.current(tabId, id);
      if (capture.confirmed.size) break;
      if (item?.state === "in_progress") localDownloading = true;
      if (!item || item.state !== "complete" || !item.exists
        || !["safe", "accepted", "allowlistedByPolicy"].includes(item.danger)
        || !Number.isSafeInteger(item.fileSize) || item.fileSize < 0) continue;
      if (![...capture.local.values()].some(record => record.id === item.id)) {
        capture.local.set(crypto.randomUUID(), { ...item }); this.changed();
      }
    }
    if (capture.local.size) return { status: "local_confirmation_required", capture_id: id };
    if (localDownloading) return { status: "downloading", capture_id: id };
    const candidates = [...capture.downloads.values()].filter(item => this.isOwnedDownload(capture, item));
    if (!candidates.length) return { status: "watching", capture_id: id };
    const metadata: Record<string, unknown>[] = [];
    for (const candidate of candidates) {
      const [item] = await this.api.downloads.search({ id: candidate.id });
      this.current(tabId, id);
      if (!item || item.state === "interrupted") continue;
      if (item.state !== "complete") {
        metadata.push({ status: "downloading", bytes_received: item.bytesReceived });
        continue;
      }
      if (!item.exists || !["safe", "accepted", "allowlistedByPolicy"].includes(item.danger)) continue;
      if (!Number.isSafeInteger(item.fileSize) || item.fileSize < 0)
        throw new Error("download_metadata_unavailable: Chrome has not reported a reliable file size");
      const previous = [...capture.candidates].find(([, record]) => record.id === item.id);
      if (previous && (previous[1].filename !== item.filename || previous[1].fileSize !== item.fileSize))
        throw new Error("download_changed: The file changed after it was registered");
      const candidateId = previous?.[0] || crypto.randomUUID();
      capture.candidates.set(candidateId, item);
      let origin = "";
      try {
        const source = new URL(item.finalUrl || item.url);
        origin = source.origin === "null" ? `${source.protocol.slice(0, -1)} download (confirmed locally)` : source.origin;
      } catch { /* No private URL in metadata. */ }
      metadata.push({ status: "ready", candidate_id: candidateId,
        name: item.filename.replace(/\\/g, "/").split("/").pop(), bytes: item.fileSize, source: origin });
    }
    if (!metadata.length) throw new Error("download_unavailable: The browser download was interrupted, removed or blocked");
    return { status: metadata.some(item => item.status === "downloading") ? "downloading"
      : metadata.length === 1 ? "ready" : "selection_required", capture_id: id, candidates: metadata };
  }

  /** Trusted backend envelope ONLY. Never expose this through the CDP method
   * switch, page messages, Agent arguments or the embedded frontend. */
  allowRead(tabId: number, id: string, candidateId: string): void {
    const capture = this.current(tabId, id);
    const selected = capture.candidates.get(candidateId);
    if (!selected) throw new Error("Unknown registered download candidate");
    if (!this.isOwnedDownload(capture, selected))
      throw new Error("download_source_changed: The selected download is no longer attributable to this tab");
    if (capture.selected && capture.selected.id !== selected.id)
      throw new Error("A transfer cannot change its approved download");
    capture.selected = selected;
    capture.readAllowed = true;
  }

  async read(tabId: number, id: string, offset: number): Promise<Record<string, unknown>> {
    // The authenticated host gates this method. No local path is accepted from
    // the Agent, web page, approval payload or download command arguments.
    const capture = this.current(tabId, id);
    if (!capture.readAllowed || !capture.selected) throw new Error("download_approval_required: No file bytes were read");
    if (capture.reading) throw new Error("A download chunk read is already in progress");
    if (offset !== capture.offset) throw new Error("Invalid download chunk offset; do not retry a consumed chunk");
    capture.reading = true;
    try {
    const tab = await this.api.tabs.get(tabId);
    this.current(tabId, id);
    if (tab.windowId !== this.windowId) throw new Error("download_window_changed");
    if (!await this.api.extension.isAllowedFileSchemeAccess()) throw new Error("file_access_revoked");
    const [item] = await this.api.downloads.search({ id: capture.selected.id });
    if (!item || !item.exists || item.state !== "complete" || !["safe", "accepted", "allowlistedByPolicy"].includes(item.danger)
      || item.filename !== capture.selected.filename || item.fileSize !== capture.selected.fileSize)
      throw new Error("download_changed: The approved file is no longer available");
    this.current(tabId, id);
    if (!capture.reader) {
      const response = await this.readFile(localFileUrl(capture.selected.filename), { signal: capture.abort.signal });
      this.current(tabId, id);
      if (!response.ok || !response.body) throw new Error("The downloaded file could not be read");
      capture.reader = response.body.getReader();
    }
    const next = capture.remainder ? { value: capture.remainder, done: false } : await capture.reader.read();
    this.current(tabId, id);
    if (next.done) {
      if (capture.offset !== capture.selected.fileSize) throw new Error("download_changed: The file size changed during transfer");
      return { data: "", eof: true, offset: capture.offset };
    }
    const chunk = next.value!.subarray(0, 192 * 1024);
    capture.remainder = next.value!.length > chunk.length ? next.value!.subarray(chunk.length) : undefined;
    if (capture.offset + chunk.length > capture.selected.fileSize) throw new Error("download_changed: The file grew during transfer");
    let binary = "";
    for (let index = 0; index < chunk.length; index += 8192) binary += String.fromCharCode(...chunk.subarray(index, index + 8192));
    capture.offset += chunk.length;
    return { data: btoa(binary), eof: false, offset: capture.offset };
    } finally { capture.reading = false; }
  }

  async end(tabId: number, id: string): Promise<void> {
    if (this.capture?.id !== id || this.capture.tabId !== tabId) return;
    await this.clear();
  }
  private async clear(): Promise<void> {
    const capture = this.capture;
    this.capture = undefined;
    this.changed();
    this.api.webRequest.onBeforeRequest.removeListener(this.onRequest);
    this.api.downloads.onCreated.removeListener(this.onCreated);
    this.api.tabs.onCreated.removeListener(this.onTab);
    this.api.tabs.onDetached.removeListener(this.onDetached);
    capture?.abort.abort();
    await capture?.reader?.cancel().catch(() => {});
    // The user's local download is never cancelled, deleted or re-downloaded.
  }
  async close(): Promise<void> { this.closed = true; await this.clear(); }
}
