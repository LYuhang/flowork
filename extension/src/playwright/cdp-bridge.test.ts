import { describe, expect, it, vi } from "vitest";
import { PlaywrightCdpBridge } from "./cdp-bridge";
import type { PlaywrightRelayChrome, RelayTab } from "./relay-executor";
import type { DownloadChrome } from "./native-downloads";

function event() {
  const listeners = new Set<(...args: any[]) => void>();
  return {
    addListener: (listener: (...args: any[]) => void) => listeners.add(listener),
    removeListener: (listener: (...args: any[]) => void) => listeners.delete(listener),
    emit: (...args: any[]) => listeners.forEach((listener) => listener(...args)),
  };
}

function fixture(downloadChrome?: DownloadChrome) {
  const tabs = new Map<number, RelayTab>([
    [10, { id: 10, windowId: 7, url: "https://example.com" }],
  ]);
  const debuggerEvent = event();
  const childInfo = new Map<string, Record<string, unknown>>();
  debuggerEvent.addListener((_source, method, params) => {
    if (method === "Target.attachedToTarget") childInfo.set(params.sessionId, params.targetInfo);
    if (method === "Target.detachedFromTarget") childInfo.delete(params.sessionId);
  });
  const debuggerDetach = event();
  const tabCreated = event();
  const tabRemoved = event();
  const tabDetached = event();
  const api: PlaywrightRelayChrome = {
    debugger: {
      attach: vi.fn(async () => undefined),
      detach: vi.fn(async () => undefined),
      sendCommand: vi.fn(async (target, method) =>
        method === "Target.getTargetInfo"
          ? { targetInfo: target.sessionId ? childInfo.get(target.sessionId)
            : { targetId: "target-10", type: "page", url: "https://example.com" } }
          : {},
      ),
      onEvent: debuggerEvent,
      onDetach: debuggerDetach,
    },
    tabs: {
      get: vi.fn(async (tabId) => tabs.get(tabId)!),
      create: vi.fn(async (properties) => ({ id: 11, windowId: Number(properties.windowId) })),
      remove: vi.fn(async () => undefined),
      onCreated: tabCreated,
      onRemoved: tabRemoved,
      onDetached: tabDetached,
    },
  };
  const events: unknown[] = [];
  const errors: unknown[] = [];
  const bridge = new PlaywrightCdpBridge(api, 7, (message) => events.push(message), error => errors.push(error),
    undefined, undefined, undefined, downloadChrome);
  bridge.initialize([tabs.get(10)!]);
  return { api, bridge, events, errors, debuggerEvent, tabCreated, tabRemoved, tabDetached, tabs };
}

describe("Playwright CDP bridge", () => {
  it("routes native metadata through an owned tab, without exposing a CDP read grant", async () => {
    const requests = event(), downloads = event();
    const item = { id: 1, url: "https://example.com/report", finalUrl: "https://example.com/report",
      filename: "/Downloads/report.csv", fileSize: 3, state: "complete", exists: true, danger: "safe",
      startTime: new Date(Date.now() + 1000).toISOString() };
    const native = {
      extension: { isAllowedFileSchemeAccess: async () => true },
      permissions: { contains: async () => true },
      tabs: { get: async () => ({ id: 10, windowId: 7 }), onCreated: event(), onDetached: event() },
      downloads: { onCreated: downloads, search: async () => [item] },
      webRequest: { onBeforeRequest: requests },
    } as unknown as DownloadChrome;
    const { bridge } = fixture(native);
    await bridge.handle({ id: 1, method: "Target.setAutoAttach" });
    const started = await bridge.handle({ id: 2, method: "Flowork.downloadBegin", sessionId: "pw-tab-1" });
    const capture_id = (started.result as { capture_id: string }).capture_id;
    requests.emit({ requestId: "r", tabId: 10, url: item.url });
    downloads.emit(item);
    const info = await bridge.handle({ id: 3, method: "Flowork.downloadInfo", sessionId: "pw-tab-1", params: { capture_id } });
    expect(info.result).toMatchObject({ status: "ready", tab_id: 10, candidates: [{ name: "report.csv" }] });
    expect(JSON.stringify(info)).not.toContain("/Downloads/");
    expect((await bridge.handle({ id: 4, method: "Flowork.downloadInfo", sessionId: "foreign", params: { capture_id } })).error).toBeDefined();
    await bridge.handle({ id: 5, method: "Flowork.allowRead", sessionId: "pw-tab-1", params: { capture_id, approved: true } });
    expect((await bridge.handle({ id: 6, method: "Flowork.downloadRead", sessionId: "pw-tab-1", params: { capture_id, offset: 0 } })).error?.message).toContain("approval_required");
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("abc"));
    try {
      const candidate_id = (info.result as { candidates: { candidate_id: string }[] }).candidates[0].candidate_id;
      const message = { id: 9, method: "Flowork.downloadRead", sessionId: "pw-tab-1", params: { capture_id, offset: 0 } };
      expect((await bridge.authorizedDownloadRead(message, { tab_id: 99, capture_id, candidate_id })).error).toBeDefined();
      expect((await bridge.authorizedDownloadRead(message, { tab_id: 10, capture_id: "other", candidate_id })).error).toBeDefined();
      expect((await bridge.authorizedDownloadRead({ ...message, params: { capture_id, offset: -1 } }, { tab_id: 10, capture_id, candidate_id })).error).toBeDefined();
      expect(fetchSpy).not.toHaveBeenCalled();
      expect((await bridge.authorizedDownloadRead(message, { tab_id: 10, capture_id, candidate_id })).result).toMatchObject({ data: "YWJj", offset: 3 });
      expect(fetchSpy).toHaveBeenCalledTimes(1);
    } finally { fetchSpy.mockRestore(); }
    expect((await bridge.handle({ id: 7, method: "Flowork.downloadEnd", sessionId: "pw-tab-1", params: { capture_id } })).result).toEqual({ ended: true, capture_id });
    expect((await bridge.handle({ id: 8, method: "Flowork.downloadInfo", sessionId: "pw-tab-1", params: { capture_id } })).error).toBeDefined();
    await bridge.close();
  });
  it("fences a popup that closes before attachment completes", async () => {
    const { bridge, api, errors, tabCreated, tabRemoved, tabs } = fixture();
    await bridge.handle({ id: 1, method: "Target.setAutoAttach" });
    let finish!: () => void;
    vi.mocked(api.debugger.attach).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const popup = { id: 12, openerTabId: 10, windowId: 7 };
    tabs.set(12, popup);
    tabCreated.emit(popup);
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    tabRemoved.emit(12);
    finish();
    await vi.waitFor(() => expect(api.debugger.detach).toHaveBeenCalledWith({ tabId: 12 }));
    await vi.waitFor(() => expect(errors).toHaveLength(1));
    expect(String(errors[0])).toContain("revoked");
    expect(bridge.attachedTabIds()).toEqual([10]);
    expect(api.debugger.sendCommand).not.toHaveBeenCalledWith({ tabId: 12 }, "Fetch.enable", expect.anything());
    expect((await bridge.handle({ id: 3, method: "Target.getTargets" })).result).toEqual({ targetInfos: [expect.objectContaining({ targetId: "target-10" })] });
    await bridge.close();
  });

  it("forgets primary and aliased targets when their tab leaves the authorized window", async () => {
    const { bridge, events, debuggerEvent, tabDetached, tabs } = fixture();
    await bridge.handle({ id: 1, method: "Target.setAutoAttach" });
    const attached = await bridge.handle({ id: 2, method: "Target.attachToTarget", params: { targetId: "target-10" } });
    const alias = (attached.result as { sessionId: string }).sessionId;
    tabs.set(10, { id: 10, windowId: 8, url: "https://example.com" });
    tabDetached.emit(10, { oldWindowId: 7 });
    expect(bridge.attachedTabIds()).toEqual([]);
    expect((await bridge.handle({ id: 3, method: "Target.getTargets" })).result).toEqual({ targetInfos: [] });
    for (const sessionId of ["pw-tab-1", alias])
      expect((await bridge.handle({ id: 4, sessionId, method: "Runtime.evaluate", params: { expression: "1" } })).error).toBeDefined();
    const count = events.length;
    debuggerEvent.emit({ tabId: 10 }, "Runtime.consoleAPICalled", { args: ["not-authorized"] });
    expect(events).toHaveLength(count);
    expect(events).toContainEqual(expect.objectContaining({ method: "Target.detachedFromTarget" }));
    await bridge.close();
  });
  it("rejects retired response interception without forwarding browser commands", async () => {
    const { bridge, api } = fixture();
    await bridge.handle({ id: 1, method: "Target.setAutoAttach" });
    vi.mocked(api.debugger.sendCommand).mockClear();
    for (const method of ["Flowork.watchPopupDownloads", "Flowork.popupDownloadCommand"]) {
      const result = await bridge.handle({ id: 2, sessionId: "pw-tab-1", method, params: { enabled: true } });
      expect(result.error?.message).toContain("retired");
    }
    expect(api.debugger.sendCommand).not.toHaveBeenCalled();
    await bridge.close();
  });

  it("rejects browser discovery/protocol escape methods on authorized page aliases", async () => {
    const { bridge, api } = fixture();
    await bridge.handle({ id: 1, method: "Target.setAutoAttach" });
    const attached = await bridge.handle({ id: 2, method: "Target.attachToTarget", params: { targetId: "target-10" } });
    const alias = (attached.result as { sessionId: string }).sessionId;
    vi.mocked(api.debugger.sendCommand).mockClear();
    for (const sessionId of ["pw-tab-1", alias]) {
      for (const method of ["Target.setDiscoverTargets", "Target.setRemoteLocations", "Target.autoAttachRelated",
        "Target.exposeDevToolsProtocol", "Target.getDevToolsTarget", "Target.openDevTools", "Target.futureBrowserMethod"]) {
        const response = await bridge.handle({ id: 3, sessionId, method, params: { targetId: "unrelated" } });
        expect(response.error?.message).toContain("explicit scoped Flowork operation");
      }
    }
    expect(api.debugger.sendCommand).not.toHaveBeenCalled();
    // Normal page operations and scoped metadata are still available.
    expect((await bridge.handle({ id: 4, sessionId: alias, method: "Runtime.evaluate", params: { expression: "1" } })).error).toBeUndefined();
    expect((await bridge.handle({ id: 5, sessionId: alias, method: "Target.getTargetInfo", params: { targetId: "unrelated" } })).result)
      .toBeUndefined();
    expect((await bridge.handle({ id: 6, sessionId: alias, method: "Target.getTargetInfo" })).result)
      .toMatchObject({ targetInfo: { targetId: "target-10" } });
  });

  it("resolves explicit target metadata without substituting targets or reviving expired sessions", async () => {
    const { bridge, api, debuggerEvent } = fixture();
    await bridge.handle({ id: 1, method: "Target.setAutoAttach" });
    const browser = await bridge.handle({ id: 2, method: "Target.attachToBrowserTarget" });
    const browserId = (browser.result as { sessionId: string }).sessionId;
    const attached = await bridge.handle({ id: 3, method: "Target.attachToTarget", params: { targetId: "target-10" } });
    const alias = (attached.result as { sessionId: string }).sessionId;
    debuggerEvent.emit({ tabId: 10 }, "Target.attachedToTarget", {
      sessionId: "frame-session", targetInfo: { targetId: "frame-target", type: "iframe" },
    });
    vi.mocked(api.debugger.sendCommand).mockClear();
    for (const sessionId of [undefined, browserId, "pw-tab-1", alias, "frame-session"]) {
      for (const targetId of ["target-10", "frame-target"])
        expect((await bridge.handle({ id: 4, sessionId, method: "Target.getTargetInfo", params: { targetId } })).result)
          .toMatchObject({ targetInfo: { targetId } });
      expect((await bridge.handle({ id: 5, sessionId, method: "Target.getTargetInfo", params: { targetId: "foreign" } })).error?.message)
        .toBe("Cannot inspect an unauthorized target");
      for (const targetId of ["", null, 42, {}])
        expect((await bridge.handle({ id: 6, sessionId, method: "Target.getTargetInfo", params: { targetId } })).error?.message)
          .toContain("nonempty targetId");
    }
    for (const sessionId of [undefined, browserId])
      expect((await bridge.handle({ id: 7, sessionId, method: "Target.getTargetInfo" })).result)
        .toEqual({ targetInfo: { targetId: "flowork-scoped-browser", type: "browser",
          title: "Flowork scoped browser", url: "", attached: true } });
    await bridge.handle({ id: 8, method: "Target.detachFromTarget", params: { sessionId: alias } });
    expect((await bridge.handle({ id: 9, sessionId: alias, method: "Target.getTargetInfo", params: { targetId: "target-10" } })).error?.message)
      .toBe("No authorized target for this session");
    debuggerEvent.emit({ tabId: 10 }, "Target.detachedFromTarget", { sessionId: "frame-session" });
    expect((await bridge.handle({ id: 10, method: "Target.getTargetInfo", params: { targetId: "frame-target" } })).error?.message)
      .toBe("Cannot inspect an unauthorized target");
    // Only authorized lookups reach Chrome, without a supplied target selector.
    for (const [target, method, params] of vi.mocked(api.debugger.sendCommand).mock.calls) {
      expect(target.tabId).toBe(10);
      expect(method).toBe("Target.getTargetInfo");
      expect(params).toEqual({});
    }
    await bridge.close();
  });

  it("refreshes committed iframe metadata instead of returning its empty attach snapshot", async () => {
    const { bridge, api, debuggerEvent } = fixture();
    await bridge.handle({ id: 1, method: "Target.setAutoAttach" });
    debuggerEvent.emit({ tabId: 10 }, "Target.attachedToTarget", {
      sessionId: "frame-session", targetInfo: { targetId: "frame-target", type: "iframe", url: "" },
    });
    const fresh = { targetId: "frame-target", type: "iframe", url: "https://other.test/committed", title: "Loaded" };
    vi.mocked(api.debugger.sendCommand).mockResolvedValue({ targetInfo: fresh });
    for (const request of [{ sessionId: "frame-session" }, { params: { targetId: "frame-target" } }])
      expect((await bridge.handle({ id: 2, method: "Target.getTargetInfo", ...request })).result)
        .toEqual({ targetInfo: fresh });
    expect(api.debugger.sendCommand).toHaveBeenLastCalledWith({ tabId: 10, sessionId: "frame-session" }, "Target.getTargetInfo", {});
    vi.mocked(api.debugger.sendCommand).mockResolvedValueOnce({ targetInfo: { targetId: "foreign", url: "private" } });
    const mismatch = await bridge.handle({ id: 3, method: "Target.getTargetInfo", params: { targetId: "frame-target" } });
    expect(mismatch.error?.message).toBe("Target metadata changed during inspection");
    expect(mismatch.result).toBeUndefined();
    await bridge.close();
  });

  it("discards refreshed metadata if the calling alias is detached while Chrome replies", async () => {
    const { bridge, api } = fixture();
    await bridge.handle({ id: 1, method: "Target.setAutoAttach" });
    const attached = await bridge.handle({ id: 2, method: "Target.attachToTarget", params: { targetId: "target-10" } });
    const alias = (attached.result as { sessionId: string }).sessionId;
    let finish!: (result: unknown) => void;
    vi.mocked(api.debugger.sendCommand).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const pending = bridge.handle({ id: 3, sessionId: alias, method: "Target.getTargetInfo" });
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    await bridge.handle({ id: 4, method: "Target.detachFromTarget", params: { sessionId: alias } });
    finish({ targetInfo: { targetId: "target-10", type: "page", url: "https://example.com/new" } });
    expect((await pending).error?.message).toBe("Target authorization changed during inspection");
    await bridge.close();
  });

  it("permits native detach only for child sessions of the owned tab", async () => {
    const { bridge, api, debuggerEvent } = fixture();
    await bridge.handle({ id: 1, method: "Target.setAutoAttach" });
    debuggerEvent.emit({ tabId: 10 }, "Target.attachedToTarget", { sessionId: "child-owned" });
    vi.mocked(api.debugger.sendCommand).mockClear();
    for (const params of [{ targetId: "unrelated" }, { sessionId: "unrelated" }, {}]) {
      expect((await bridge.handle({ id: 2, sessionId: "pw-tab-1", method: "Target.detachFromTarget", params })).error?.message)
        .toContain("unauthorized child session");
    }
    expect(api.debugger.sendCommand).not.toHaveBeenCalled();
    expect((await bridge.handle({ id: 3, sessionId: "pw-tab-1", method: "Target.detachFromTarget",
      params: { sessionId: "child-owned", targetId: "unrelated" } })).error).toBeUndefined();
    expect(api.debugger.sendCommand).toHaveBeenLastCalledWith({ tabId: 10, sessionId: undefined },
      "Target.detachFromTarget", { sessionId: "child-owned" });
  });

  it("supports Playwright newCDPSession browser aliases without granting global Chrome access", async () => {
    const { bridge, api, events } = fixture();
    await bridge.handle({ id: 1, method: "Target.setAutoAttach" });
    const browser = await bridge.handle({ id: 2, method: "Target.attachToBrowserTarget" });
    const rootId = (browser.result as { sessionId: string }).sessionId;
    expect(rootId).toMatch(/^pw-browser-/);
    events.length = 0;
    const attached = await bridge.handle({ id: 3, sessionId: rootId, method: "Target.attachToTarget", params: { targetId: "target-10", flatten: true } });
    const pageId = (attached.result as { sessionId: string }).sessionId;
    expect(events).toContainEqual({ sessionId: rootId, method: "Target.attachedToTarget", params: {
      sessionId: pageId, targetInfo: { targetId: "target-10", type: "page", url: "https://example.com", attached: true }, waitingForDebugger: false,
    } });
    expect((await bridge.handle({ id: 4, sessionId: pageId, method: "Flowork.tabInfo" })).result).toMatchObject({ target_id: "target-10" });
    for (const method of ["Browser.close", "Storage.getCookies", "Runtime.evaluate", "Target.createBrowserContext"]) {
      expect((await bridge.handle({ id: 5, sessionId: rootId, method })).error?.message).toContain("not permitted");
    }
    expect((await bridge.handle({ id: 6, sessionId: rootId, method: "Target.attachToTarget", params: { targetId: "unrelated" } })).error?.message).toContain("unauthorized");
    expect(api.debugger.sendCommand).toHaveBeenCalledTimes(1);
    await bridge.handle({ id: 7, sessionId: rootId, method: "Target.detachFromTarget", params: { sessionId: pageId } });
    expect(events).toContainEqual({ sessionId: rootId, method: "Target.detachedFromTarget", params: { sessionId: pageId } });
    await bridge.handle({ id: 8, method: "Target.detachFromTarget", params: { sessionId: rootId } });
    expect((await bridge.handle({ id: 9, sessionId: rootId, method: "Target.getTargets" })).error).toBeDefined();
  });

  it("routes Fetch pauses only to their owner and releases interception before alias detach", async () => {
    const { bridge, events, debuggerEvent, api } = fixture();
    await bridge.handle({ id: 1, method: "Target.setAutoAttach" });
    const attached = await bridge.handle({ id: 2, method: "Target.attachToTarget", params: { targetId: "target-10" } });
    const sessionId = (attached.result as { sessionId: string }).sessionId;
    expect((await bridge.handle({ id: 3, sessionId, method: "Fetch.enable", params: { patterns: [] } })).error).toBeUndefined();
    events.length = 0;
    debuggerEvent.emit({ tabId: 10 }, "Fetch.requestPaused", { requestId: "download" });
    expect(events).toEqual([{ sessionId, method: "Fetch.requestPaused", params: { requestId: "download" } }]);
    const conflict = await bridge.handle({ id: 4, sessionId: "pw-tab-1", method: "Fetch.continueRequest", params: { requestId: "download" } });
    expect(conflict.error?.message).toContain("another CDP session");
    await bridge.handle({ id: 5, method: "Target.detachFromTarget", params: { sessionId } });
    expect(api.debugger.sendCommand).toHaveBeenLastCalledWith({ tabId: 10, sessionId: undefined }, "Fetch.disable", {});
    expect((await bridge.handle({ id: 6, sessionId: "pw-tab-1", method: "Fetch.enable" })).error).toBeUndefined();
  });

  it("provides a browser version without asking the page", async () => {
    const { bridge } = fixture();
    const response = await bridge.handle({ id: 1, method: "Browser.getVersion" });
    expect(response).toMatchObject({
      id: 1,
      result: { protocolVersion: "1.3" },
    });
  });

  it("turns Target.setAutoAttach into approved-tab debugger attachment", async () => {
    const { api, bridge, events } = fixture();
    expect(await bridge.handle({ id: 2, method: "Target.setAutoAttach", params: {} }))
      .toEqual({ id: 2, sessionId: undefined, result: {} });
    expect(api.debugger.attach).toHaveBeenCalledWith({ tabId: 10 }, "1.3");
    expect(events).toContainEqual({
      method: "Target.attachedToTarget",
      params: {
        sessionId: "pw-tab-1",
        targetInfo: {
          targetId: "target-10",
          type: "page",
          url: "https://example.com",
          attached: true,
        },
        waitingForDebugger: false,
      },
    });
  });

  it("routes page and child-session commands without losing session identity", async () => {
    const { api, bridge, debuggerEvent } = fixture();
    await bridge.handle({ id: 3, method: "Target.setAutoAttach", params: {} });
    debuggerEvent.emit(
      { tabId: 10 },
      "Target.attachedToTarget",
      { sessionId: "child-1" },
    );
    await bridge.handle({
      id: 4,
      sessionId: "child-1",
      method: "Runtime.evaluate",
      params: { expression: "1 + 1" },
    });
    expect(api.debugger.sendCommand).toHaveBeenLastCalledWith(
      { tabId: 10, sessionId: "child-1" },
      "Runtime.evaluate",
      { expression: "1 + 1" },
    );
  });

  it("returns protocol errors instead of throwing across the transport", async () => {
    const { bridge } = fixture();
    const response = await bridge.handle({
      id: 5,
      sessionId: "missing",
      method: "Runtime.evaluate",
      params: {},
    });
    expect(response.error?.message).toContain("No tab found for sessionId");
  });

  it("hydrates an existing OOPIF navigation once without replacing a newer live navigation", async () => {
    const { api, bridge, debuggerEvent, events } = fixture();
    await bridge.handle({ id: 1, method: "Target.setAutoAttach" });
    debuggerEvent.emit({ tabId: 10 }, "Target.attachedToTarget", { sessionId: "child-1" });
    const frame = { id: "frame-child", parentId: "target-10", url: "https://other.test/frame", name: "cross" };
    vi.mocked(api.debugger.sendCommand).mockResolvedValue({ frameTree: { frame } });
    events.length = 0;
    await bridge.handle({ id: 2, sessionId: "child-1", method: "Page.getFrameTree" });
    expect(events).toEqual([{ sessionId: "child-1", method: "Page.frameNavigated", params: { frame, type: "Navigation" } }]);
    events.length = 0;
    await bridge.handle({ id: 3, sessionId: "child-1", method: "Page.getFrameTree" });
    expect(events).toEqual([]);

    debuggerEvent.emit({ tabId: 10 }, "Target.attachedToTarget", { sessionId: "child-2" });
    vi.mocked(api.debugger.sendCommand).mockImplementationOnce(async () => {
      debuggerEvent.emit({ tabId: 10, sessionId: "child-2" }, "Page.frameNavigated", { frame: { ...frame, url: "https://other.test/new" } });
      return { frameTree: { frame } };
    });
    events.length = 0;
    await bridge.handle({ id: 4, sessionId: "child-2", method: "Page.getFrameTree" });
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ params: { frame: { url: "https://other.test/new" } } });
  });

  it("routes additional sessions only to live iframe targets within the owned tab", async () => {
    const { api, bridge, debuggerEvent, events } = fixture();
    await bridge.handle({ id: 1, method: "Target.setAutoAttach" });
    debuggerEvent.emit({ tabId: 10 }, "Target.attachedToTarget", {
      sessionId: "native-frame", targetInfo: { targetId: "frame-target", type: "iframe", url: "https://other.test/frame" },
    });
    debuggerEvent.emit({ tabId: 10 }, "Target.attachedToTarget", {
      sessionId: "native-worker", targetInfo: { targetId: "worker-target", type: "service_worker" },
    });
    expect((await bridge.handle({ id: 2, method: "Target.attachToTarget", params: { targetId: "worker-target" } })).error).toBeDefined();
    expect((await bridge.handle({ id: 3, method: "Target.attachToTarget", params: { targetId: "unrelated-frame" } })).error).toBeDefined();
    const attached = await bridge.handle({ id: 4, method: "Target.attachToTarget", params: { targetId: "frame-target" } });
    const sessionId = (attached.result as { sessionId: string }).sessionId;
    await bridge.handle({ id: 5, sessionId, method: "Runtime.evaluate", params: { expression: "location.href" } });
    expect(api.debugger.sendCommand).toHaveBeenLastCalledWith({ tabId: 10, sessionId: "native-frame" }, "Runtime.evaluate", { expression: "location.href" });
    expect((await bridge.handle({ id: 6, sessionId, method: "Target.getTargetInfo" })).result).toMatchObject({ targetInfo: { targetId: "frame-target" } });
    events.length = 0;
    debuggerEvent.emit({ tabId: 10 }, "Runtime.consoleAPICalled", { marker: "main" });
    expect(events).not.toContainEqual(expect.objectContaining({ sessionId }));
    debuggerEvent.emit({ tabId: 10, sessionId: "native-frame" }, "Runtime.consoleAPICalled", { marker: "frame" });
    expect(events).toContainEqual({ sessionId, method: "Runtime.consoleAPICalled", params: { marker: "frame" } });
    await bridge.handle({ id: 7, sessionId, method: "Fetch.enable" });
    events.length = 0;
    debuggerEvent.emit({ tabId: 10, sessionId: "native-frame" }, "Fetch.requestPaused", { requestId: "frame-download" });
    expect(events).toEqual([{ sessionId, method: "Fetch.requestPaused", params: { requestId: "frame-download" } }]);
    await bridge.handle({ id: 8, method: "Target.detachFromTarget", params: { sessionId } });
    expect(api.debugger.sendCommand).toHaveBeenLastCalledWith({ tabId: 10, sessionId: "native-frame" }, "Fetch.disable", {});
    expect((await bridge.handle({ id: 9, sessionId: "native-frame", method: "Runtime.evaluate" })).error).toBeUndefined();

    const second = await bridge.handle({ id: 10, method: "Target.attachToTarget", params: { targetId: "frame-target" } });
    const secondId = (second.result as { sessionId: string }).sessionId;
    debuggerEvent.emit({ tabId: 10 }, "Target.detachedFromTarget", { sessionId: "native-frame" });
    expect((await bridge.handle({ id: 11, sessionId: secondId, method: "Runtime.evaluate" })).error).toBeDefined();
    expect((await bridge.handle({ id: 12, method: "Target.attachToTarget", params: { targetId: "frame-target" } })).error).toBeDefined();
  });

  it("only creates additional CDP sessions for already owned targets", async () => {
    const { bridge, api, events, debuggerEvent } = fixture();
    await bridge.handle({ id: 1, method: "Target.setAutoAttach", params: {} });
    const denied = await bridge.handle({ id: 2, method: "Target.attachToTarget", params: { targetId: "unrelated" } });
    expect(denied.error?.message).toContain("unauthorized");
    const attached = await bridge.handle({ id: 3, method: "Target.attachToTarget", params: { targetId: "target-10", flatten: true } });
    const sessionId = (attached.result as { sessionId: string }).sessionId;
    expect(sessionId).toMatch(/^pw-extra-/);
    await bridge.handle({ id: 4, sessionId, method: "Runtime.evaluate", params: { expression: "1+1" } });
    expect(api.debugger.sendCommand).toHaveBeenLastCalledWith({ tabId: 10 }, "Runtime.evaluate", { expression: "1+1" });
    debuggerEvent.emit({ tabId: 10 }, "Network.responseReceived", { requestId: "req" });
    expect(events).toContainEqual({ sessionId, method: "Network.responseReceived", params: { requestId: "req" } });
    const metadata = await bridge.handle({ id: 5, sessionId, method: "Flowork.tabInfo" });
    expect(metadata.result).toMatchObject({ target_id: "target-10", window_id: "win_7" });
    await bridge.handle({ id: 6, method: "Target.detachFromTarget", params: { sessionId } });
    const stale = await bridge.handle({ id: 7, sessionId, method: "Runtime.evaluate" });
    expect(stale.error).toBeDefined();
  });

  it("does not forward browser-wide commands or cookies through a page target", async () => {
    const { bridge, api } = fixture();
    await bridge.handle({ id: 1, method: "Target.setAutoAttach" });
    for (const method of ["Browser.close", "Storage.getCookies", "Network.getAllCookies", "Network.getCookies", "Target.attachToTarget", "Target.createBrowserContext"]) {
      const root = await bridge.handle({ id: 2, method, params: { targetId: "unrelated" } });
      expect(root.error, method).toBeDefined();
      const child = await bridge.handle({ id: 3, sessionId: "pw-tab-1", method, params: { targetId: "unrelated" } });
      expect(child.error, method).toBeDefined();
    }
    expect(api.debugger.sendCommand).toHaveBeenCalledTimes(1);
  });
});
