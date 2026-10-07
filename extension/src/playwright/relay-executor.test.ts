import { describe, expect, it, vi } from "vitest";
import {
  PLAYWRIGHT_RELAY_ALLOWED_COMMANDS,
  PlaywrightRelayExecutor,
  type PlaywrightRelayChrome,
  type RelayTab,
} from "./relay-executor";

type Listener<T extends (...args: never[]) => void> = T;

function event<T extends (...args: never[]) => void>() {
  const listeners = new Set<Listener<T>>();
  return {
    addListener: (listener: T) => listeners.add(listener),
    removeListener: (listener: T) => listeners.delete(listener),
    emit: (...args: Parameters<T>) => {
      for (const listener of listeners) listener(...args);
    },
    count: () => listeners.size,
  };
}

function fixture(onOwnedTabDetached = vi.fn(), onAttachedTabsChanged = vi.fn(), quotedTabWindows = new Map<number, number>()) {
  const tabs = new Map<number, RelayTab>([
    [10, { id: 10, windowId: 7, title: "Allowed" }],
    [11, { id: 11, windowId: 8, title: "Other window" }],
  ]);
  const debuggerEvent = event<any>();
  const debuggerDetach = event<any>();
  const tabCreated = event<any>();
  const tabRemoved = event<any>();
  const tabDetached = event<any>();
  const api: PlaywrightRelayChrome = {
    debugger: {
      attach: vi.fn(async () => undefined),
      detach: vi.fn(async () => undefined),
      sendCommand: vi.fn(async () => ({ value: "ok" })),
      onEvent: debuggerEvent,
      onDetach: debuggerDetach,
    },
    tabs: {
      get: vi.fn(async (tabId) => {
        const tab = tabs.get(tabId);
        if (!tab) throw new Error("missing tab");
        return tab;
      }),
      create: vi.fn(async (properties) => {
        const tab = { id: 12, windowId: Number(properties.windowId) };
        tabs.set(12, tab);
        return tab;
      }),
      remove: vi.fn(async () => undefined),
      onCreated: tabCreated,
      onRemoved: tabRemoved,
      onDetached: tabDetached,
    },
  };
  const messages: unknown[] = [];
  const relay = new PlaywrightRelayExecutor(
    api,
    7,
    (message) => messages.push(message),
    onOwnedTabDetached,
    onAttachedTabsChanged,
    quotedTabWindows,
  );
  return {
    api,
    tabs,
    relay,
    messages,
    debuggerEvent,
    debuggerDetach,
    tabCreated,
    tabRemoved,
    tabDetached,
    onOwnedTabDetached,
    onAttachedTabsChanged,
  };
}

describe("Playwright extension relay", () => {
  it('allows the exact quoted tab in another window, but not its neighbors or a moved tab', async () => {
    const { relay, tabs, tabDetached, api } = fixture(undefined, undefined, new Map([[11, 8]]));
    tabs.set(13, { id: 13, windowId: 8 });
    const attach = (tabId: number) => relay.handle({ id: tabId, method: 'chrome.debugger.attach', params: [{ tabId }, '1.3'] });
    expect((await attach(11)).error).toBeUndefined();
    expect((await attach(13)).error?.message).toContain('outside');
    tabDetached.emit(11, { oldWindowId: 8 });
    tabs.set(11, { id: 11, windowId: 9 });
    expect((await attach(11)).error).toBeDefined();
    expect(api.debugger.attach).toHaveBeenCalledTimes(1);
    await relay.close();
  });
  async function attached() {
    const f = fixture();
    await f.relay.handle({ id: 100, method: "chrome.debugger.attach", params: [{ tabId: 10 }, "1.3"] });
    const command = (method: string, parameters: Record<string, unknown> = {}, sessionId?: string) =>
      f.relay.handle({ id: 101, method: "chrome.debugger.sendCommand", params: [{ tabId: 10, ...(sessionId ? { sessionId } : {}) }, method, parameters] });
    return { ...f, command };
  }

  it.each(["success", "window move", "tab removed", "debugger detached", "relay closed"])(
    "fences popup events until post-attach window validation on %s", async outcome => {
      const { relay, api, tabs, tabCreated, tabDetached, tabRemoved, debuggerEvent, debuggerDetach, messages } = await attached();
      const popup = { id: 12, windowId: 7, openerTabId: 10, url: "https://example.com/export" };
      tabs.set(12, popup);
      tabCreated.emit(popup);
      let checked = 0;
      let finishCheck!: (value: RelayTab) => void;
      vi.mocked(api.tabs.get).mockImplementation(async tabId => {
        if (tabId === 12 && ++checked === 2) {
          debuggerEvent.emit({ tabId }, "Runtime.consoleAPICalled", { args: ["not-authorized-yet"] });
          return new Promise(resolve => { finishCheck = resolve; });
        }
        return tabs.get(tabId)!;
      });
      const pending = relay.handle({ id: 1, method: "chrome.debugger.attach", params: [{ tabId: 12 }, "1.3"] });
      await vi.waitFor(() => expect(finishCheck).toBeTypeOf("function"));
      expect(api.debugger.sendCommand).not.toHaveBeenCalledWith({ tabId: 12 }, "Fetch.enable", expect.anything());
      expect(relay.attachedTabIds()).toEqual([10]);
      expect(messages.some((message: any) => message.method === "chrome.debugger.onEvent")).toBe(false);
      if (outcome === "window move") tabDetached.emit(12, { oldWindowId: 7 });
      if (outcome === "tab removed") tabRemoved.emit(12);
      if (outcome === "debugger detached") debuggerDetach.emit({ tabId: 12 }, "target_closed");
      if (outcome === "relay closed") await relay.close();
      finishCheck(popup);
      const result = await pending;
      debuggerEvent.emit({ tabId: 12 }, "Runtime.consoleAPICalled", { args: ["after-validation"] });
      const forwarded = messages.filter((message: any) => message.method === "chrome.debugger.onEvent");
      if (outcome === "success") {
        expect(result.error).toBeUndefined();
        expect(forwarded).toEqual([{ method: "chrome.debugger.onEvent", params: [
          { tabId: 12 }, "Runtime.consoleAPICalled", { args: ["after-validation"] },
        ] }]);
      } else {
        expect(result.error).toBeDefined();
        expect(forwarded).toEqual([]);
        expect(api.debugger.detach).toHaveBeenCalledWith({ tabId: 12 });
        expect(relay.attachedTabIds()).not.toContain(12);
      }
      await relay.close();
    },
  );

  it("rejects retired early-capture options before native attachment", async () => {
    const { relay, api } = fixture();
    const result = await relay.handle({ id: 1, method: "chrome.debugger.attach", params: [
      { tabId: 10 }, "1.3", { captureResponses: true },
    ] });
    expect(result.error?.message).toContain("retired");
    expect(api.debugger.attach).not.toHaveBeenCalled();
    expect(api.debugger.sendCommand).not.toHaveBeenCalled();
    await relay.close();
  });

  it("revokes events, in-flight replies and future commands when a tab leaves its window", async () => {
    const { relay, api, tabs, tabDetached, debuggerEvent, messages, command, onOwnedTabDetached } = await attached();
    let finish!: (value: unknown) => void;
    vi.mocked(api.debugger.sendCommand).mockImplementation(async (_target, method) =>
      method === "Runtime.evaluate" ? new Promise(resolve => { finish = resolve; }) : {});
    const pending = command("Runtime.evaluate", { expression: "readLater()" });
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    tabs.set(10, { id: 10, windowId: 8 });
    tabDetached.emit(10, { oldWindowId: 7 });
    const count = messages.length;
    debuggerEvent.emit({ tabId: 10 }, "Runtime.consoleAPICalled", { args: ["foreign-window-data"] });
    debuggerEvent.emit({ tabId: 10, sessionId: "child" }, "Network.responseReceived", { response: { url: "https://private.example" } });
    expect(messages).toHaveLength(count);
    expect(relay.attachedTabIds()).toEqual([]);
    expect(onOwnedTabDetached).toHaveBeenCalledWith(10, "tab_moved_out_of_window");
    finish({ value: "late-private-data" });
    expect((await pending).error?.message).toContain("revoked");
    expect((await command("Runtime.evaluate")).error?.message).toContain("uncontrolled tab");
    await vi.waitFor(() => expect(api.debugger.detach).toHaveBeenCalledWith({ tabId: 10 }));
    tabs.set(10, { id: 10, windowId: 7 });
    expect((await relay.handle({ id: 3, method: "chrome.debugger.attach", params: [{ tabId: 10 }, "1.3"] })).error?.message).toContain("left the authorized window");
    await relay.close();
    expect(tabDetached.count()).toBe(0);
  });

  it.each([
    ["DOM.setFileInputFiles", { files: ["/nonexistent-flowork-fixture.bin"], nodeId: 1 }],
    ["DOM.getFileInfo", { objectId: "fixture-file" }],
    ["Network.loadNetworkResource", { url: "file:///nonexistent-flowork-fixture.bin", options: {} }],
    ["Network.loadNetworkResource", { url: "  fI\nLe:///nonexistent-flowork-fixture.bin", options: {} }],
    ["Page.setDownloadBehavior", { behavior: "allow", downloadPath: "/nonexistent-flowork-downloads" }],
    ["Input.dispatchDragEvent", { type: "drop", x: 0, y: 0, data: { items: [], files: ["/nonexistent-flowork-fixture.bin"], dragOperationsMask: 1 } }],
  ])("rejects browser-local filesystem access via %s on primary and child sessions", async (method, parameters) => {
    const { relay, api, command } = await attached();
    for (const session of [undefined, "child-session"]) {
      const before = vi.mocked(api.debugger.sendCommand).mock.calls.length;
      const result = await command(method as string, parameters as Record<string, unknown>, session);
      expect(result.error?.message).toContain("Browser-local file paths are not available");
      expect(vi.mocked(api.debugger.sendCommand).mock.calls).toHaveLength(before);
    }
    await relay.close();
  });

  it("preserves text-only native drag operations", async () => {
    const { relay, api, command } = await attached();
    const parameters = { type: "drop", x: 3, y: 5, data: {
      items: [{ mimeType: "text/plain", data: "sandbox-independent text" }], files: [], dragOperationsMask: 1,
    } };
    expect((await command("Input.dispatchDragEvent", parameters)).error).toBeUndefined();
    expect(api.debugger.sendCommand).toHaveBeenCalledWith({ tabId: 10 }, "Input.dispatchDragEvent", parameters);
    await relay.close();
  });

  it("does not publish an attachment that raced with a window move", async () => {
    const { relay, api, tabs, tabDetached, onAttachedTabsChanged } = fixture();
    let finish!: () => void;
    vi.mocked(api.debugger.attach).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const pending = relay.handle({ id: 1, method: "chrome.debugger.attach", params: [{ tabId: 10 }, "1.3"] });
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    tabs.set(10, { id: 10, windowId: 8 });
    tabDetached.emit(10, { oldWindowId: 7 });
    // Even moving back before the native attach resolves cannot revive the old grant.
    tabs.set(10, { id: 10, windowId: 7 });
    finish();
    expect((await pending).error?.message).toContain("revoked");
    expect(relay.attachedTabIds()).toEqual([]);
    expect(onAttachedTabsChanged.mock.calls.some(([, reason]) => reason === "attached")).toBe(false);
    expect(api.debugger.detach).toHaveBeenCalledWith({ tabId: 10 });
    await relay.close();
  });

  it("forwards removal during attach and never publishes the late result", async () => {
    const { relay, api, tabRemoved, messages, onAttachedTabsChanged } = fixture();
    let finish!: () => void;
    vi.mocked(api.debugger.attach).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const pending = relay.handle({ id: 1, method: "chrome.debugger.attach", params: [{ tabId: 10 }, "1.3"] });
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    tabRemoved.emit(11); // Foreign removals must remain invisible.
    expect(messages).toEqual([]);
    tabRemoved.emit(10);
    expect(messages).toEqual([{ method: "chrome.tabs.onRemoved", params: [10] }]);
    // Keep stale tabs.get metadata to prove the terminal event itself fences
    // the late reply, independently of another asynchronous Chrome lookup.
    finish();
    expect((await pending).error?.message).toContain("revoked");
    expect(relay.attachedTabIds()).toEqual([]);
    expect(onAttachedTabsChanged.mock.calls.some(([, reason]) => reason === "attached")).toBe(false);
    expect(api.debugger.detach).toHaveBeenCalledWith({ tabId: 10 });
    await relay.close();
  });

  it("forwards the removal of an announced popup after its attachment already failed", async () => {
    const { relay, api, tabs, tabCreated, tabRemoved, messages } = await attached();
    const popup = { id: 12, windowId: 7, openerTabId: 10 };
    tabs.set(12, popup);
    tabCreated.emit(popup);
    vi.mocked(api.debugger.attach).mockRejectedValue(new Error("Tab vanished during attach"));
    const result = await relay.handle({ id: 1, method: "chrome.debugger.attach", params: [{ tabId: 12 }, "1.3"] });
    expect(result.error?.message).toContain("vanished");
    tabRemoved.emit(12);
    tabRemoved.emit(12);
    expect(messages.filter((message: any) => message.method === "chrome.tabs.onRemoved"))
      .toEqual([{ method: "chrome.tabs.onRemoved", params: [12] }]);
    expect(relay.attachedTabIds()).toEqual([10]);
    await relay.close();
  });

  it("releases held keys and buttons at the latest coordinates before detach", async () => {
    const { relay, api, command } = await attached();
    await command("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "Shift", code: "ShiftLeft", windowsVirtualKeyCode: 16, modifiers: 8 });
    await command("Input.dispatchMouseEvent", { type: "mousePressed", button: "left", x: 10, y: 20 });
    await command("Input.dispatchMouseEvent", { type: "mouseMoved", x: 90, y: 110 });
    await command("Runtime.evaluate", { expression: "1" });
    vi.mocked(api.debugger.detach).mockImplementation(async () => {
      expect(api.debugger.sendCommand).toHaveBeenCalledWith({ tabId: 10 }, "Input.dispatchKeyEvent", {
        type: "keyUp", key: "Shift", code: "ShiftLeft", windowsVirtualKeyCode: 16, modifiers: 0,
      });
      expect(api.debugger.sendCommand).toHaveBeenCalledWith({ tabId: 10 }, "Input.dispatchMouseEvent", {
        type: "mouseReleased", button: "left", buttons: 0, x: 90, y: 110, modifiers: 0, clickCount: 1,
      });
    });
    await Promise.all([relay.close(), relay.close()]);
    expect(api.debugger.detach).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.debugger.sendCommand).mock.calls.some(call => call[1] === "Runtime.terminateExecution")).toBe(false);
  });

  it("does not release keys/buttons already released by the Agent", async () => {
    const { relay, api, command } = await attached();
    await command("Input.dispatchKeyEvent", { type: "keyDown", key: "a", code: "KeyA" });
    await command("Input.dispatchKeyEvent", { type: "keyUp", key: "a", code: "KeyA" });
    await command("Input.dispatchMouseEvent", { type: "mousePressed", button: "right", x: 1, y: 2 });
    await command("Input.dispatchMouseEvent", { type: "mouseReleased", button: "right", x: 1, y: 2 });
    vi.mocked(api.debugger.sendCommand).mockClear();
    await relay.close();
    expect(api.debugger.sendCommand).not.toHaveBeenCalled();
  });

  it("terminates only in-flight script targets, including child frames, before detach", async () => {
    const { relay, api, command } = await attached();
    let endScript!: (value: unknown) => void;
    let started!: () => void;
    const scriptStarted = new Promise<void>(resolve => { started = resolve; });
    vi.mocked(api.debugger.sendCommand).mockImplementation(async (target, method) => {
      if (method === "Runtime.callFunctionOn") {
        started();
        return new Promise(resolve => { endScript = resolve; });
      }
      if (method === "Runtime.terminateExecution") {
        expect(target).toEqual({ tabId: 10, sessionId: "child" });
        endScript({ exceptionDetails: { text: "Execution terminated" } });
      }
      return {};
    });
    const script = command("Runtime.callFunctionOn", { functionDeclaration: "() => new Promise(() => {})", awaitPromise: true }, "child");
    await scriptStarted;
    await relay.close();
    await script;
    expect(api.debugger.sendCommand).toHaveBeenCalledWith({ tabId: 10, sessionId: "child" }, "Runtime.terminateExecution", {});
    expect(api.debugger.detach).toHaveBeenCalledWith({ tabId: 10 });
  });

  it("bounds cleanup even when renderer termination and input release hang", async () => {
    vi.useFakeTimers();
    try {
      const { relay, api, command } = await attached();
      await command("Input.dispatchKeyEvent", { type: "keyDown", key: "a", code: "KeyA" });
      let endScript!: (value: unknown) => void;
      let started!: () => void;
      const scriptStarted = new Promise<void>(resolve => { started = resolve; });
      vi.mocked(api.debugger.sendCommand).mockImplementation(async (_target, method) => {
        if (method === "Runtime.evaluate") {
          started();
          return new Promise(resolve => { endScript = resolve; });
        }
        return new Promise(() => {});
      });
      const script = command("Runtime.evaluate", { expression: "never" });
      await scriptStarted;
      const closing = relay.close();
      await vi.advanceTimersByTimeAsync(2001);
      await closing;
      expect(api.debugger.detach).toHaveBeenCalledTimes(1);
      endScript({});
      await script;
    } finally { vi.useRealTimers(); }
  });

  it("cleans held input on explicit detach as well as connection close", async () => {
    const { relay, api, command } = await attached();
    await command("Input.dispatchKeyEvent", { type: "rawKeyDown", code: "ControlLeft", key: "Control" });
    await relay.handle({ id: 102, method: "chrome.debugger.detach", params: [{ tabId: 10 }] });
    expect(api.debugger.sendCommand).toHaveBeenLastCalledWith({ tabId: 10 }, "Input.dispatchKeyEvent", {
      type: "keyUp", code: "ControlLeft", key: "Control", modifiers: 0,
    });
    await relay.close();
    expect(api.debugger.detach).toHaveBeenCalledTimes(1);
  });

  it("cannot dispatch a command whose window check completed after close", async () => {
    const { relay, api, command } = await attached();
    let finishLookup!: (value: RelayTab) => void;
    vi.mocked(api.tabs.get).mockImplementation(() => new Promise(resolve => { finishLookup = resolve; }));
    const pending = command("Runtime.evaluate", { expression: "mutate()" });
    await relay.close();
    finishLookup({ id: 10, windowId: 7, url: "https://example.com" });
    expect((await pending).error?.message).toContain("closing");
    expect(api.debugger.sendCommand).not.toHaveBeenCalled();
  });

  it.each(["chrome://settings", "chrome-extension://test/sidepanel.html", "devtools://devtools", "edge://settings"])("rejects control of privileged UI %s", async url => {
    const { relay, tabs, api, command } = await attached();
    tabs.set(10, { id: 10, windowId: 7, url: "https://example.com", pendingUrl: url });
    expect((await command("Runtime.evaluate", { expression: "grant()" })).error?.message).toContain("cannot be controlled");
    expect(api.debugger.sendCommand).not.toHaveBeenCalled();
    await relay.close();
  });

  it("keeps the upstream command surface deliberately small", () => {
    expect([...PLAYWRIGHT_RELAY_ALLOWED_COMMANDS].sort()).toEqual([
      "chrome.debugger.attach",
      "chrome.debugger.detach",
      "chrome.debugger.sendCommand",
      "chrome.tabs.create",
      "chrome.tabs.get",
      "chrome.tabs.remove",
    ]);
  });

  it("advertises only selected tabs in the side-panel window", () => {
    const { relay, messages } = fixture();
    relay.initialize([
      { id: 10, windowId: 7 },
      { id: 11, windowId: 8 },
    ]);
    expect(messages).toEqual([
      { method: "chrome.tabs.onCreated", params: [{ id: 10, windowId: 7 }] },
      { method: "extension.initialized", params: [] },
    ]);
  });

  it("attaches and forwards CDP only for an allowed tab", async () => {
    const { relay, api } = fixture();
    expect(await relay.handle({
      id: 1,
      method: "chrome.debugger.attach",
      params: [{ tabId: 10 }, "1.3"],
    })).toEqual({ id: 1, result: {} });
    expect(relay.attachedTabIds()).toEqual([10]);

    expect(await relay.handle({
      id: 2,
      method: "chrome.debugger.sendCommand",
      params: [{ tabId: 10 }, "Runtime.evaluate", { expression: "1 + 1" }],
    })).toEqual({ id: 2, result: { value: "ok" } });
    expect(api.debugger.sendCommand).toHaveBeenCalledWith(
      { tabId: 10 },
      "Runtime.evaluate",
      { expression: "1 + 1" },
    );

    await relay.handle({
      id: 9,
      method: "chrome.debugger.sendCommand",
      params: [
        { tabId: 10, sessionId: "child-session" },
        "Runtime.evaluate",
        { expression: "2 + 2" },
      ],
    });
    expect(api.debugger.sendCommand).toHaveBeenLastCalledWith(
      { tabId: 10, sessionId: "child-session" },
      "Runtime.evaluate",
      { expression: "2 + 2" },
    );
  });

  it("rejects tabs outside the side-panel window and uncontrolled tabs", async () => {
    const { relay, api } = fixture();
    const outside = await relay.handle({
      id: 3,
      method: "chrome.debugger.attach",
      params: [{ tabId: 11 }, "1.3"],
    });
    expect(outside.error?.message).toContain("outside the side-panel window");
    expect(api.debugger.attach).not.toHaveBeenCalled();

    const uncontrolled = await relay.handle({
      id: 4,
      method: "chrome.debugger.sendCommand",
      params: [{ tabId: 10 }, "Runtime.evaluate", {}],
    });
    expect(uncontrolled.error?.message).toContain("uncontrolled tab");
  });

  it("forwards events only for attached tabs and same-window popups", async () => {
    const { relay, messages, debuggerEvent, tabCreated, tabRemoved } = fixture();
    await relay.handle({
      id: 5,
      method: "chrome.debugger.attach",
      params: [{ tabId: 10 }, "1.3"],
    });
    debuggerEvent.emit({ tabId: 11 }, "Runtime.consoleAPICalled", {});
    debuggerEvent.emit({ tabId: 10 }, "Runtime.consoleAPICalled", { type: "log" });
    tabCreated.emit({ id: 12, windowId: 7, openerTabId: 10 });
    tabCreated.emit({ id: 13, windowId: 8, openerTabId: 10 });
    tabRemoved.emit(11);
    tabRemoved.emit(10);

    expect(messages).toEqual([
      {
        method: "chrome.debugger.onEvent",
        params: [{ tabId: 10 }, "Runtime.consoleAPICalled", { type: "log" }],
      },
      {
        method: "chrome.tabs.onCreated",
        params: [{ id: 12, windowId: 7, openerTabId: 10 }],
      },
      { method: "chrome.tabs.onRemoved", params: [10] },
    ]);
  });

  it("reports an unexpected debugger detach to the session control plane", async () => {
    const {
      relay,
      debuggerDetach,
      onOwnedTabDetached,
      onAttachedTabsChanged,
    } = fixture();
    await relay.handle({
      id: 51,
      method: "chrome.debugger.attach",
      params: [{ tabId: 10 }, "1.3"],
    });
    debuggerDetach.emit({ tabId: 10 }, "canceled_by_user");

    expect(onOwnedTabDetached).toHaveBeenCalledWith(10, "canceled_by_user");
    expect(onAttachedTabsChanged).toHaveBeenLastCalledWith(
      [],
      "detached",
      10,
    );
    expect(relay.attachedTabIds()).toEqual([]);
  });

  it("projects attached and removed tab ownership to durable session state", async () => {
    const { relay, tabRemoved, onAttachedTabsChanged } = fixture();
    await relay.handle({
      id: 52,
      method: "chrome.debugger.attach",
      params: [{ tabId: 10 }, "1.3"],
    });
    expect(onAttachedTabsChanged).toHaveBeenLastCalledWith(
      [10],
      "attached",
      10,
    );

    tabRemoved.emit(10);
    expect(onAttachedTabsChanged).toHaveBeenLastCalledWith(
      [],
      "tab_removed",
      10,
    );
  });

  it("forces new tabs into the side-panel window", async () => {
    const { relay, api } = fixture();
    expect(await relay.handle({
      id: 6,
      method: "chrome.tabs.create",
      params: [{ url: "https://example.com", windowId: 99 }],
    })).toEqual({ id: 6, result: { id: 12, windowId: 7 } });
    expect(api.tabs.create).toHaveBeenCalledWith({
      url: "https://example.com",
      windowId: 7,
    });
  });

  it("rejects arbitrary chrome methods", async () => {
    const { relay } = fixture();
    const result = await relay.handle({
      id: 7,
      method: "chrome.history.search",
      params: [],
    });
    expect(result.error).toEqual({
      code: -32600,
      message: "Unsupported Playwright relay method: chrome.history.search",
    });
  });

  it("detaches controlled tabs and removes listeners on close", async () => {
    const { relay, api, debuggerEvent, tabCreated } = fixture();
    await relay.handle({
      id: 8,
      method: "chrome.debugger.attach",
      params: [{ tabId: 10 }, "1.3"],
    });
    await relay.close();
    expect(api.debugger.detach).toHaveBeenCalledWith({ tabId: 10 });
    expect(debuggerEvent.count()).toBe(0);
    expect(tabCreated.count()).toBe(0);
  });
});
