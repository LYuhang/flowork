import { describe, expect, it, vi } from "vitest";
import { localFileUrl, NativeDownloads, type DownloadChrome } from "./native-downloads";

function event() {
  const listeners = new Set<(...args: any[]) => void>();
  return {
    addListener: (listener: (...args: any[]) => void) => listeners.add(listener),
    removeListener: (listener: (...args: any[]) => void) => listeners.delete(listener),
    emit: (...args: any[]) => listeners.forEach(listener => listener(...args)),
    listeners,
  };
}

function fixture() {
  const requests = event(), downloads = event(), tabs = event(), detached = event();
  const items = new Map<number, Record<string, unknown>>();
  const api = {
    extension: { isAllowedFileSchemeAccess: vi.fn(async () => true) },
    permissions: { contains: vi.fn(async () => true) },
    tabs: { get: vi.fn(async () => ({ id: 1, windowId: 7 })), onCreated: tabs, onDetached: detached },
    downloads: { onCreated: downloads, search: vi.fn(async ({ id }) => items.has(id) ? [items.get(id)] : []) },
    webRequest: { onBeforeRequest: requests },
  };
  const read = vi.fn(async () => new Response("abc"));
  const owner = { live: true };
  const manager = new NativeDownloads(api as unknown as DownloadChrome, 7, id => owner.live && id === 1, read);
  const download = (id: number, tabId = 1, url = `https://example.com/export/${id}`) => {
    requests.emit({ requestId: `request-${id}`, tabId, url });
    const item = { id, url, finalUrl: url, filename: `/downloads/report-${id}.txt`, fileSize: 3,
      state: "complete", exists: true, startTime: new Date(Date.now() + 1).toISOString(), danger: "safe" };
    items.set(id, item);
    downloads.emit(item);
  };
  return { manager, api, read, owner, requests, downloads, tabs, detached, items, download };
}

describe("native browser downloads", () => {
  it("keeps Blob/data metadata local until explicit confirmation, then still requires transfer approval", async () => {
    for (const url of ["blob:https://example.com/opaque", "data:text/plain,abc"]) {
      const f = fixture(); const { capture_id: id } = await f.manager.begin(1);
      f.download(10, 999, url); // URL/time are deliberately not proof of tab ownership.
      expect(await f.manager.info(1, String(id))).toEqual({ status: "local_confirmation_required", capture_id: id });
      const local = f.manager.localConfirmations()!;
      expect(local.files[0].name).toBe("report-10.txt");
      expect(JSON.stringify(local)).not.toContain("/downloads/");
      expect(() => f.manager.allowRead(1, String(id), local.files[0].id)).toThrow("Unknown");
      await f.manager.confirmLocal(String(id), local.files[0].id);
      expect(f.manager.localConfirmations()).toBeNull();
      const info = await f.manager.info(1, String(id)); expect(info.status).toBe("ready");
      if (url.startsWith("data:")) expect(info.candidates).toMatchObject([{ source: "data download (confirmed locally)" }]);
      await expect(f.manager.read(1, String(id), 0)).rejects.toThrow("approval_required");
      f.manager.allowRead(1, String(id), (info.candidates as { candidate_id: string }[])[0].candidate_id);
      expect(await f.manager.read(1, String(id), 0)).toMatchObject({ data: "YWJj" });
      await f.manager.close();
    }
  });

  it("cancels local confirmation without deleting files and rejects stale decisions", async () => {
    const f = fixture(); const { capture_id: id } = await f.manager.begin(1);
    f.download(10, 999, "data:text/plain,abc"); await f.manager.info(1, String(id));
    const file = f.manager.localConfirmations()!.files[0].id;
    await f.manager.confirmLocal(String(id), null);
    expect(f.manager.localConfirmations()).toBeNull();
    await expect(f.manager.info(1, String(id))).rejects.toThrow("download_local_cancelled");
    await f.manager.end(1, String(id)); await f.manager.begin(1);
    await expect(f.manager.confirmLocal(String(id), file)).rejects.toThrow("no longer active");
    expect(f.items.has(10)).toBe(true); expect(f.read).not.toHaveBeenCalled();
    await f.manager.close();
  });

  it("promotes Chrome's incomplete onCreated record to the exact confirmed completed file", async () => {
    const f = fixture(); const { capture_id: id } = await f.manager.begin(1);
    const initial = { id: 10, url: "blob:https://example.com/opaque", finalUrl: "blob:https://example.com/opaque",
      filename: "", fileSize: -1, state: "in_progress", exists: true,
      startTime: new Date(Date.now() + 1).toISOString(), danger: "safe" };
    f.items.set(10, initial); f.downloads.emit(initial);
    expect(await f.manager.info(1, String(id))).toMatchObject({ status: "downloading" });
    f.items.set(10, { ...initial, filename: "/downloads/blob.bin", fileSize: 3, state: "complete" });
    expect(await f.manager.info(1, String(id))).toMatchObject({ status: "local_confirmation_required" });
    await f.manager.confirmLocal(String(id), f.manager.localConfirmations()!.files[0].id);
    const info = await f.manager.info(1, String(id));
    expect(info).toMatchObject({ status: "ready", candidates: [{ name: "blob.bin", bytes: 3 }] });
    f.manager.allowRead(1, String(id), (info.candidates as { candidate_id: string }[])[0].candidate_id);
    expect(await f.manager.read(1, String(id), 0)).toMatchObject({ data: "YWJj" });
    await f.manager.close();
  });

  it("revalidates exact local file and window at confirmation time", async () => {
    for (const change of ["file", "window", "owner", "close"] as const) {
      const f = fixture(); const { capture_id: id } = await f.manager.begin(1);
      f.download(10, 999, "blob:https://example.com/opaque"); await f.manager.info(1, String(id));
      const file = f.manager.localConfirmations()!.files[0].id;
      if (change === "file") f.items.get(10)!.filename = "/downloads/replaced.txt";
      if (change === "window") f.api.tabs.get.mockResolvedValue({ id: 1, windowId: 8 });
      if (change === "owner") f.owner.live = false;
      if (change === "close") await f.manager.close();
      await expect(f.manager.confirmLocal(String(id), file)).rejects.toThrow();
      expect(f.read).not.toHaveBeenCalled(); await f.manager.close();
    }
  });
  it("calls the native fetch with its global receiver rather than the download manager", async () => {
    const f = fixture();
    const nativeFetch = vi.spyOn(globalThis, "fetch").mockImplementation(async function (this: typeof globalThis) {
      expect(this).toBe(globalThis);
      return new Response("abc");
    });
    const manager = new NativeDownloads(f.api as unknown as DownloadChrome, 7, id => id === 1);
    try {
      const { capture_id } = await manager.begin(1);
      f.download(10);
      const info = await manager.info(1, String(capture_id));
      manager.allowRead(1, String(capture_id), (info.candidates as { candidate_id: string }[])[0].candidate_id);
      expect(await manager.read(1, String(capture_id), 0)).toMatchObject({ data: "YWJj", offset: 3 });
    } finally {
      await manager.close();
      nativeFetch.mockRestore();
    }
  });
  it("encodes local paths without treating filename punctuation as URL syntax", () => {
    expect(localFileUrl("/Downloads/report #1%.csv")).toBe("file:///Downloads/report%20%231%25.csv");
    expect(localFileUrl("C:\\Downloads\\a.txt")).toBe("file:///C:/Downloads/a.txt");
    expect(() => localFileUrl("relative.txt")).toThrow("absolute");
    expect(() => localFileUrl("\\\\server\\share\\file")).toThrow("Network-share");
  });

  it("requires the user's first-use file permission before observing or triggering downloads", async () => {
    const f = fixture();
    f.api.extension.isAllowedFileSchemeAccess.mockResolvedValue(false);
    expect(await f.manager.begin(1)).toMatchObject({ error: "file_access_required" });
    expect(f.downloads.listeners.size).toBe(0);
    expect(f.read).not.toHaveBeenCalled();
  });

  it("cleans up a partial listener installation and allows a clean retry", async () => {
    const f = fixture();
    const install = f.api.downloads.onCreated.addListener;
    f.api.downloads.onCreated.addListener = () => { throw new Error("Permission revoked during setup"); };
    await expect(f.manager.begin(1)).rejects.toThrow("Permission revoked");
    expect(f.requests.listeners.size).toBe(0);
    f.api.downloads.onCreated.addListener = install;
    expect(await f.manager.begin(1)).toMatchObject({ status: "watching" });
    await f.manager.close();
  });

  it("requires all observation permissions without touching file bytes", async () => {
    const f = fixture();
    f.api.permissions.contains.mockResolvedValue(false);
    expect(await f.manager.begin(1)).toMatchObject({ error: "download_permissions_required" });
    expect(f.requests.listeners.size).toBe(0);
    expect(f.read).not.toHaveBeenCalled();
  });

  it("registers opaque candidates, but never reads bytes before trusted approval", async () => {
    const f = fixture();
    const { capture_id: id } = await f.manager.begin(1);
    f.download(10);
    const info = await f.manager.info(1, String(id));
    expect(JSON.stringify(info)).not.toContain("/downloads/");
    const candidates = info.candidates as { candidate_id: string }[];
    expect(info.status).toBe("ready");
    await expect(f.manager.read(1, String(id), 0)).rejects.toThrow("approval_required");
    expect(f.read).not.toHaveBeenCalled();
    f.manager.allowRead(1, String(id), candidates[0].candidate_id);
    expect(await f.manager.read(1, String(id), 0)).toEqual({ data: "YWJj", eof: false, offset: 3 });
    expect(await f.manager.read(1, String(id), 3)).toEqual({ data: "", eof: true, offset: 3 });
    await f.manager.close();
    expect(f.items.has(10)).toBe(true); // No deletion/re-download on cleanup.
    expect(f.downloads.listeners.size).toBe(0);
  });

  it("returns multiple proven owned candidates without guessing which one the user intended", async () => {
    const f = fixture();
    const { capture_id: id } = await f.manager.begin(1);
    f.download(10); f.download(11);
    const info = await f.manager.info(1, String(id));
    expect(info.status).toBe("selection_required");
    expect(info.candidates).toHaveLength(2);
    expect((await f.manager.info(1, String(id))).candidates).toEqual(info.candidates);
    expect(f.read).not.toHaveBeenCalled();
  });

  it("never returns a different tab's file or guesses when owned and foreign requests share a URL", async () => {
    const f = fixture();
    const { capture_id: id } = await f.manager.begin(1);
    f.download(10, 999);
    expect(await f.manager.info(1, String(id))).toMatchObject({ status: "watching" });
    f.requests.emit({ requestId: "owned-same-url", tabId: 1, url: "https://example.com/export/10" });
    await expect(f.manager.info(1, String(id))).rejects.toThrow("ambiguous");
    expect(f.read).not.toHaveBeenCalled();
  });

  it("observes an opener-owned popup even when it disappears before debugger attachment", async () => {
    const f = fixture();
    const { capture_id: id } = await f.manager.begin(1);
    f.tabs.emit({ id: 2, openerTabId: 1, windowId: 7 });
    f.download(10, 2);
    // No live popup/get or debugger attachment is required for metadata.
    expect(await f.manager.info(1, String(id))).toMatchObject({ status: "ready" });
    expect(f.api.tabs.get).toHaveBeenLastCalledWith(1);
  });

  it("rejects a candidate that becomes ambiguous while approval is pending", async () => {
    const f = fixture();
    const { capture_id: id } = await f.manager.begin(1);
    f.download(10);
    const info = await f.manager.info(1, String(id));
    f.requests.emit({ requestId: "foreign-late", tabId: 999, url: "https://example.com/export/10" });
    expect(() => f.manager.allowRead(1, String(id), (info.candidates as { candidate_id: string }[])[0].candidate_id))
      .toThrow("download_ambiguous");
    expect(f.read).not.toHaveBeenCalled();
    await f.manager.close();
  });

  it("rechecks attribution after approval and while an asynchronous file read is pending", async () => {
    for (const duringRead of [false, true]) {
      const f = fixture();
      const { capture_id: id } = await f.manager.begin(1);
      f.download(10);
      const info = await f.manager.info(1, String(id));
      f.manager.allowRead(1, String(id), (info.candidates as { candidate_id: string }[])[0].candidate_id);
      const foreign = () => f.requests.emit({ requestId: "foreign-late", tabId: 999, url: "https://example.com/export/10" });
      if (duringRead) f.read.mockImplementation(async () => { foreign(); return new Response("abc"); });
      else foreign();
      await expect(f.manager.read(1, String(id), 0)).rejects.toThrow("download_ambiguous");
      expect(f.read).toHaveBeenCalledTimes(duringRead ? 1 : 0);
      await f.manager.close();
    }
  });

  it("checks the current window even when its detach notification has not arrived", async () => {
    const f = fixture();
    const { capture_id: id } = await f.manager.begin(1);
    f.download(10);
    const info = await f.manager.info(1, String(id));
    f.manager.allowRead(1, String(id), (info.candidates as { candidate_id: string }[])[0].candidate_id);
    f.api.tabs.get.mockResolvedValue({ id: 1, windowId: 8 });
    await expect(f.manager.read(1, String(id), 0)).rejects.toThrow("download_window_changed");
    expect(f.read).not.toHaveBeenCalled();
    await f.manager.close();
  });

  it("revokes reads on window movement, permission revocation and owner loss", async () => {
    for (const revoke of ["window", "permission", "owner"] as const) {
      const f = fixture();
      const { capture_id: id } = await f.manager.begin(1);
      f.download(10);
      const info = await f.manager.info(1, String(id));
      f.manager.allowRead(1, String(id), (info.candidates as { candidate_id: string }[])[0].candidate_id);
      if (revoke === "window") f.detached.emit(1);
      if (revoke === "permission") f.api.extension.isAllowedFileSchemeAccess.mockResolvedValue(false);
      if (revoke === "owner") f.owner.live = false;
      await expect(f.manager.read(1, String(id), 0)).rejects.toThrow();
      expect(f.read).not.toHaveBeenCalled();
    }
  });
});

it("URL downloads bind Chrome's ID, not matching URLs, and require transfer approval", async () => {
  const f = fixture(); const start = vi.fn(async () => {
    f.download(10, -1, "https://cdn.example.com/a.png");
    f.download(11, 999, "https://cdn.example.com/a.png"); return 10;
  });
  Object.assign(f.api.downloads, { download: start });
  const { capture_id } = await f.manager.begin(1, true); const id = String(capture_id);
  for (const url of ["file:///private", "blob:https://example.com/id", "data:text/plain,a", "https://u:p@example.com/a", "bad"])
    await expect(f.manager.startUrl(1, id, url)).rejects.toThrow(/download_url/);
  f.owner.live = false;
  await expect(f.manager.startUrl(1, id, "https://cdn.example.com/a.png")).rejects.toThrow("authorized");
  expect(start).not.toHaveBeenCalled(); f.owner.live = true;
  await f.manager.startUrl(1, id, "https://cdn.example.com/a.png");
  expect(start).toHaveBeenCalledWith({ url: "https://cdn.example.com/a.png", saveAs: false, conflictAction: "uniquify" });
  const info = await f.manager.info(1, id);
  expect(info.status).toBe("ready");
  const candidates = info.candidates as { candidate_id: string; name: string }[];
  expect(candidates.map(x => x.name)).toEqual(["report-10.txt"]);
  await expect(f.manager.read(1, id, 0)).rejects.toThrow("approval_required"); expect(f.read).not.toHaveBeenCalled();
  f.manager.allowRead(1, id, candidates[0].candidate_id);
  expect(await f.manager.read(1, id, 0)).toMatchObject({ data: "YWJj" });
  await expect(f.manager.startUrl(1, id, "https://cdn.example.com/a.png")).rejects.toThrow("already_started");
  expect(start).toHaveBeenCalledTimes(1);
  f.items.get(10)!.state = "interrupted"; f.items.get(10)!.error = "SERVER_FORBIDDEN";
  await expect(f.manager.info(1, id)).rejects.toThrow("SERVER_FORBIDDEN");
  await f.manager.close();
});

it("a late URL start cannot attach its file to a replacement capture", async () => {
  const f = fixture(); let finish!: (id: number) => void;
  const start = vi.fn(() => new Promise<number>(resolve => { finish = resolve; }));
  Object.assign(f.api.downloads, { download: start });
  const first = await f.manager.begin(1, true);
  const pending = f.manager.startUrl(1, String(first.capture_id), "https://example.com/image");
  await vi.waitFor(() => expect(start).toHaveBeenCalledTimes(1));
  await f.manager.end(1, String(first.capture_id));
  const second = await f.manager.begin(1, true); finish(10);
  await expect(pending).rejects.toThrow("no longer active");
  expect(await f.manager.info(1, String(second.capture_id))).toMatchObject({ status: "watching" });
  await f.manager.close();
});
