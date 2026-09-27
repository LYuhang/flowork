/**
 * Copyright (c) Microsoft Corporation.
 * Copyright (c) Flowork contributors.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * The data-plane half of Playwright's extension relay protocol v2, adapted to
 * Flowork's authenticated WebSocket transport.
 *
 * Upstream reference:
 *   microsoft/playwright packages/extension/src/relayConnection.ts
 *   commit 680e5ad5894a54bba9e4ed8a311fd2aee388137d
 *
 * This module deliberately does not implement DOM, locator, snapshot, wait, or
 * action semantics. Playwright owns those on the server. The extension only
 * invokes a six-command chrome.* allow-list and forwards events for tabs that
 * belong to the user-approved side-panel window.
 */

export type PlaywrightRelayRequest = {
  id: number;
  method: string;
  params?: unknown[];
};

export type PlaywrightRelayMessage = {
  id?: number;
  method?: string;
  params?: unknown[];
  result?: unknown;
  error?: { code: number; message: string };
};

export type RelayTab = {
  id?: number;
  windowId: number;
  openerTabId?: number;
  title?: string;
  url?: string;
  pendingUrl?: string;
  active?: boolean;
};

export type RelayDebuggee = { tabId?: number; sessionId?: string };

type TargetActivity = {
  target: RelayDebuggee;
  pendingScripts: number;
  keys: Map<string, Record<string, unknown>>;
  buttons: Set<string>;
  x: number;
  y: number;
};

export type PlaywrightRelayChrome = {
  debugger: {
    attach(target: RelayDebuggee, version: string): Promise<void>;
    detach(target: RelayDebuggee): Promise<void>;
    sendCommand(
      target: RelayDebuggee,
      method: string,
      params?: Record<string, unknown>,
    ): Promise<unknown>;
    onEvent: {
      addListener(
        listener: (
          source: RelayDebuggee,
          method: string,
          params?: Record<string, unknown>,
        ) => void,
      ): void;
      removeListener(
        listener: (
          source: RelayDebuggee,
          method: string,
          params?: Record<string, unknown>,
        ) => void,
      ): void;
    };
    onDetach: {
      addListener(listener: (source: RelayDebuggee, reason: string) => void): void;
      removeListener(listener: (source: RelayDebuggee, reason: string) => void): void;
    };
  };
  tabs: {
    get(tabId: number): Promise<RelayTab>;
    create(properties: Record<string, unknown>): Promise<RelayTab>;
    remove(tabId: number | number[]): Promise<void>;
    onCreated: {
      addListener(listener: (tab: RelayTab) => void): void;
      removeListener(listener: (tab: RelayTab) => void): void;
    };
    onRemoved: {
      addListener(listener: (tabId: number) => void): void;
      removeListener(listener: (tabId: number) => void): void;
    };
    onDetached: {
      addListener(listener: (tabId: number, info: { oldWindowId: number }) => void): void;
      removeListener(listener: (tabId: number, info: { oldWindowId: number }) => void): void;
    };
  };
};

export const PLAYWRIGHT_RELAY_ALLOWED_COMMANDS = new Set([
  "chrome.debugger.attach",
  "chrome.debugger.detach",
  "chrome.debugger.sendCommand",
  "chrome.tabs.create",
  "chrome.tabs.remove",
  "chrome.tabs.get",
]);

const JSON_RPC_INVALID_REQUEST = -32600;
const JSON_RPC_INTERNAL_ERROR = -32603;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function tabIdFrom(value: unknown): number {
  const tabId = Number((value as RelayDebuggee | undefined)?.tabId);
  if (!Number.isInteger(tabId) || tabId < 0)
    throw new Error("Playwright relay command requires a valid tabId");
  return tabId;
}

export class PlaywrightRelayExecutor {
  private readonly attachedTabs = new Set<number>();
  private readonly announcedTabs = new Set<number>();
  private readonly removeListeners: Array<() => void> = [];
  private readonly activity = new Map<string, TargetActivity>();
  private readonly detachingTabs = new Set<number>();
  private readonly attachingTabs = new Set<number>();
  private readonly revokedTabs = new Set<number>();
  private closed = false;
  private closing: Promise<void> | undefined;

  constructor(
    private readonly api: PlaywrightRelayChrome,
    private readonly windowId: number,
    private readonly emit: (message: PlaywrightRelayMessage) => void,
    private readonly onOwnedTabDetached: (
      tabId: number,
      reason: string,
    ) => void = () => undefined,
    private readonly onAttachedTabsChanged: (
      tabIds: number[],
      reason: "attached" | "detached" | "tab_removed",
      tabId: number,
    ) => void = () => undefined,
  ) {
    const onDebuggerEvent = (
      source: RelayDebuggee,
      method: string,
      params?: Record<string, unknown>,
    ) => {
      if (source.tabId === undefined || this.closed || this.revokedTabs.has(source.tabId) || this.detachingTabs.has(source.tabId)) return;
      const message: PlaywrightRelayMessage = {
        method: "chrome.debugger.onEvent",
        params: [source, method, params ?? {}],
      };
      if (this.attachedTabs.has(source.tabId)) this.emit(message);
    };
    const onDebuggerDetach = (source: RelayDebuggee, reason: string) => {
      if (source.tabId === undefined || (!this.attachedTabs.has(source.tabId) && !this.attachingTabs.has(source.tabId))) return;
      this.revokedTabs.add(source.tabId);
      this.attachedTabs.delete(source.tabId);
      this.forgetActivity(source.tabId);
      this.emit({ method: "chrome.debugger.onDetach", params: [source, reason] });
      this.notifyTabsChanged("detached", source.tabId);
      this.onOwnedTabDetached(source.tabId, reason);
    };
    const onTabCreated = (tab: RelayTab) => {
      if (
        tab.windowId !== this.windowId ||
        tab.openerTabId === undefined ||
        !this.attachedTabs.has(tab.openerTabId)
      ) return;
      if (tab.id !== undefined) this.announcedTabs.add(tab.id);
      this.emit({ method: "chrome.tabs.onCreated", params: [tab] });
    };
    const onTabRemoved = (tabId: number) => {
      if (!this.attachedTabs.has(tabId) && !this.attachingTabs.has(tabId) && !this.announcedTabs.has(tabId)) return;
      // A download-only popup may disappear before debugger.attach resolves.
      // Forward its terminal event now and fence the pending attachment; the
      // download owner must not wait for a tab which no longer exists.
      this.revokedTabs.add(tabId);
      this.announcedTabs.delete(tabId);
      this.attachedTabs.delete(tabId);
      this.forgetActivity(tabId);
      this.emit({ method: "chrome.tabs.onRemoved", params: [tabId] });
      this.notifyTabsChanged("tab_removed", tabId);
    };
    const onTabDetached = (tabId: number, info: { oldWindowId: number }) => {
      if (info.oldWindowId !== this.windowId ||
          (!this.attachedTabs.has(tabId) && !this.attachingTabs.has(tabId) && !this.announcedTabs.has(tabId))) return;
      // Revoke synchronously before any cleanup await: both events and commands
      // must stop at the window boundary, including an attach currently in flight.
      this.revokedTabs.add(tabId);
      this.announcedTabs.delete(tabId);
      const attached = this.attachedTabs.delete(tabId);
      this.emit({ method: "chrome.debugger.onDetach", params: [{ tabId }, "tab_moved_out_of_window"] });
      this.notifyTabsChanged("detached", tabId);
      this.onOwnedTabDetached(tabId, "tab_moved_out_of_window");
      if (attached) {
        void this.cleanupTab(tabId).finally(() => this.api.debugger.detach({ tabId })).catch(() => {});
      }
    };

    api.debugger.onEvent.addListener(onDebuggerEvent);
    api.debugger.onDetach.addListener(onDebuggerDetach);
    api.tabs.onCreated.addListener(onTabCreated);
    api.tabs.onRemoved.addListener(onTabRemoved);
    api.tabs.onDetached.addListener(onTabDetached);
    this.removeListeners.push(
      () => api.debugger.onEvent.removeListener(onDebuggerEvent),
      () => api.debugger.onDetach.removeListener(onDebuggerDetach),
      () => api.tabs.onCreated.removeListener(onTabCreated),
      () => api.tabs.onRemoved.removeListener(onTabRemoved),
      () => api.tabs.onDetached.removeListener(onTabDetached),
    );
  }

  /**
   * Advertise only tabs already selected by Flowork's browser-session control
   * plane. The Playwright relay responds by asking the extension to attach.
   */
  initialize(tabs: RelayTab[]): void {
    if (this.closed) throw new Error("Playwright relay is closed");
    for (const tab of tabs) {
      if (tab.id === undefined || tab.windowId !== this.windowId) continue;
      this.announcedTabs.add(tab.id);
      this.emit({ method: "chrome.tabs.onCreated", params: [tab] });
    }
    this.emit({ method: "extension.initialized", params: [] });
  }

  async handle(request: PlaywrightRelayRequest): Promise<PlaywrightRelayMessage> {
    if (this.closed) {
      return {
        id: request.id,
        error: { code: JSON_RPC_INTERNAL_ERROR, message: "Playwright relay is closed" },
      };
    }
    if (
      !Number.isInteger(request.id) ||
      typeof request.method !== "string" ||
      !PLAYWRIGHT_RELAY_ALLOWED_COMMANDS.has(request.method) ||
      (request.params !== undefined && !Array.isArray(request.params))
    ) {
      return {
        id: request.id,
        error: {
          code: JSON_RPC_INVALID_REQUEST,
          message: `Unsupported Playwright relay method: ${String(request.method)}`,
        },
      };
    }

    try {
      return { id: request.id, result: await this.invoke(request) };
    } catch (error) {
      return {
        id: request.id,
        error: { code: JSON_RPC_INTERNAL_ERROR, message: errorMessage(error) },
      };
    }
  }

  async close(): Promise<void> {
    if (this.closing) return this.closing;
    this.closed = true;
    for (const remove of this.removeListeners.splice(0)) remove();
    const tabs = [...this.attachedTabs];
    this.attachedTabs.clear();
    this.announcedTabs.clear();
    this.closing = Promise.allSettled(tabs.map(async tabId => {
      await this.cleanupTab(tabId);
      await this.api.debugger.detach({ tabId });
    })).then(() => undefined);
    return this.closing;
  }

  attachedTabIds(): number[] {
    return [...this.attachedTabs];
  }

  private forgetActivity(tabId: number): void {
    for (const [key, state] of this.activity)
      if (state.target.tabId === tabId) this.activity.delete(key);
  }

  private activityFor(target: RelayDebuggee): TargetActivity {
    const key = `${target.tabId}:${target.sessionId ?? "main"}`;
    let state = this.activity.get(key);
    if (!state) {
      state = { target, pendingScripts: 0, keys: new Map(), buttons: new Set(), x: 0, y: 0 };
      this.activity.set(key, state);
    }
    return state;
  }

  /** Cleanup deadlines only bound teardown, never normal command execution. */
  private async cleanupTab(tabId: number): Promise<void> {
    const states = [...this.activity.values()].filter(state => state.target.tabId === tabId);
    this.forgetActivity(tabId);
    const bounded = async (operations: Promise<unknown>[]) => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        await Promise.race([
          Promise.allSettled(operations),
          new Promise<void>(resolve => { timer = setTimeout(resolve, 1000); }),
        ]);
      } finally { clearTimeout(timer); }
    };
    // Only interrupt evaluations still owned by this relay. Do not issue page
    // reloads or claim to undo completed scripts or page-owned timers.
    await bounded(states.filter(state => state.pendingScripts > 0).map(state =>
      this.api.debugger.sendCommand(state.target, "Runtime.terminateExecution", {})));
    await bounded(states.flatMap(state => [
      ...[...state.keys.values()].map(key => this.api.debugger.sendCommand(
        state.target, "Input.dispatchKeyEvent", { ...key, type: "keyUp", modifiers: 0 },
      )),
      ...[...state.buttons].map(button => this.api.debugger.sendCommand(
        state.target, "Input.dispatchMouseEvent",
        { type: "mouseReleased", button, buttons: 0, x: state.x, y: state.y, modifiers: 0, clickCount: 1 },
      )),
    ]));
  }

  private async sendTracked(
    target: RelayDebuggee, method: string, parameters: Record<string, unknown>,
  ): Promise<unknown> {
    const state = this.activityFor(target);
    const script = ["Runtime.evaluate", "Runtime.callFunctionOn", "Runtime.awaitPromise"].includes(method);
    if (script) state.pendingScripts++;
    const keyId = String(parameters.code || parameters.key || parameters.windowsVirtualKeyCode || "");
    if (method === "Input.dispatchKeyEvent" && ["keyDown", "rawKeyDown"].includes(String(parameters.type))) {
      const key: Record<string, unknown> = {};
      for (const field of ["key", "code", "windowsVirtualKeyCode", "nativeVirtualKeyCode", "location", "isKeypad"])
        if (parameters[field] !== undefined) key[field] = parameters[field];
      state.keys.set(keyId, key);
    }
    if (method === "Input.dispatchMouseEvent") {
      if (typeof parameters.x === "number") state.x = parameters.x;
      if (typeof parameters.y === "number") state.y = parameters.y;
      if (parameters.type === "mousePressed") state.buttons.add(String(parameters.button));
    }
    try {
      const result = await this.api.debugger.sendCommand(target, method, parameters);
      if (this.closed || this.revokedTabs.has(target.tabId!) || this.detachingTabs.has(target.tabId!) || !this.attachedTabs.has(target.tabId!))
        throw new Error("Playwright relay target was revoked before the command completed");
      if (method === "Input.dispatchKeyEvent" && parameters.type === "keyUp") state.keys.delete(keyId);
      if (method === "Input.dispatchMouseEvent" && parameters.type === "mouseReleased") state.buttons.delete(String(parameters.button));
      return result ?? {};
    } finally {
      if (script) state.pendingScripts--;
    }
  }

  private async requireTabInWindow(tabId: number): Promise<RelayTab> {
    const tab = await this.api.tabs.get(tabId);
    if (tab.windowId !== this.windowId)
      throw new Error("Playwright relay target is outside the side-panel window");
    if ([tab.url, tab.pendingUrl].some(url => /^(chrome|chrome-extension|devtools|edge):/i.test(url || "")))
      throw new Error("Browser and extension UI cannot be controlled by the Agent");
    return tab;
  }

  private async invoke(request: PlaywrightRelayRequest): Promise<unknown> {
    const params = request.params ?? [];
    switch (request.method) {
      case "chrome.debugger.attach": {
        const target = params[0] as RelayDebuggee;
        const tabId = tabIdFrom(target);
        if (params.length > 2) throw new Error("Early response capture has been retired");
        if (this.revokedTabs.has(tabId)) throw new Error("Playwright relay target left the authorized window");
        this.attachingTabs.add(tabId);
        let physicallyAttached = false;
        try {
          await this.requireTabInWindow(tabId);
          if (this.closed || this.detachingTabs.has(tabId) || this.revokedTabs.has(tabId)) throw new Error("Playwright relay is closing or the target was revoked");
          await this.api.debugger.attach({ tabId }, String(params[1] || "1.3"));
          physicallyAttached = true;
          if (this.closed || this.revokedTabs.has(tabId)) throw new Error("Playwright relay is closed or the target was revoked");
          await this.requireTabInWindow(tabId);
          if (this.closed || this.revokedTabs.has(tabId)) throw new Error("Playwright relay is closed or the target was revoked");
          this.attachedTabs.add(tabId);
          this.notifyTabsChanged("attached", tabId);
          return {};
        } catch (error) {
          if (physicallyAttached) await this.api.debugger.detach({ tabId }).catch(() => {});
          throw error;
        } finally {
          this.attachingTabs.delete(tabId);
        }
      }
      case "chrome.debugger.detach": {
        const tabId = tabIdFrom(params[0]);
        if (!this.attachedTabs.has(tabId))
          throw new Error("Playwright relay cannot detach an uncontrolled tab");
        this.detachingTabs.add(tabId);
        try {
          await this.cleanupTab(tabId);
          await this.api.debugger.detach({ tabId });
          if (this.attachedTabs.delete(tabId)) this.notifyTabsChanged("detached", tabId);
        } finally { this.detachingTabs.delete(tabId); }
        return {};
      }
      case "chrome.debugger.sendCommand": {
        const tabId = tabIdFrom(params[0]);
        if (!this.attachedTabs.has(tabId))
          throw new Error("Playwright relay cannot address an uncontrolled tab");
        await this.requireTabInWindow(tabId);
        if (this.closed || this.detachingTabs.has(tabId) || !this.attachedTabs.has(tabId))
          throw new Error("Playwright relay is closing");
        const source = params[0] as RelayDebuggee;
        const method = String(params[1] || "");
        const parameters = (params[2] as Record<string, unknown> | undefined) ?? {};
        const dragFiles = (parameters.data as { files?: unknown } | undefined)?.files;
        const localResource = method === "Network.loadNetworkResource" &&
          new URL(String(parameters.url)).protocol === "file:";
        // CLI paths belong to the sandbox. Raw CDP must not reinterpret them
        // as paths on the user's computer (read, reveal, or write local files).
        // Upload/drop adapters transfer bytes and do not need these methods.
        if (localResource || ["DOM.setFileInputFiles", "DOM.getFileInfo", "Page.setDownloadBehavior"].includes(method) ||
            (method === "Input.dispatchDragEvent" && dragFiles !== undefined &&
              (!Array.isArray(dragFiles) || dragFiles.length > 0)))
          throw new Error("Browser-local file paths are not available. Use browser upload/drop or sandbox-backed setInputFiles instead.");
        // Raw CDP must not turn an authorized tab into authority over browser
        // profiles, unrelated targets or credentials from unrelated sites.
        // Target is an authority-management domain, not an ordinary page API.
        // Deny new/unknown methods by default (including discovery, remote
        // locations and exposing a browser protocol binding inside a page).
        // The model scopes target metadata and child-session detach above us.
        const allowedTargetMethods = ["Target.getTargetInfo", "Target.setAutoAttach", "Target.detachFromTarget"];
        if ((method.startsWith("Target.") && !allowedTargetMethods.includes(method)) ||
            /^(Browser\.|Storage\.|Network\.(getAllCookies|getCookies|setCookie|setCookies|deleteCookies|clearBrowserCookies))/.test(method))
          throw new Error("Browser-wide targets, storage and credentials require an explicit scoped Flowork operation");
        if (method === "Page.navigate") {
          const url = String((params[2] as { url?: string })?.url || "");
          if (!/^https?:\/\//i.test(url) && url !== "about:blank")
            throw new Error("Agent navigation requires HTTP(S) or about:blank");
        }
        return this.sendTracked(
          source.sessionId ? { tabId, sessionId: source.sessionId } : { tabId },
          method,
          parameters,
        );
      }
      case "chrome.tabs.get": {
        const tabId = tabIdFrom(params[0]);
        if (!this.attachedTabs.has(tabId))
          throw new Error("Playwright relay cannot inspect an uncontrolled tab");
        return this.requireTabInWindow(tabId);
      }
      case "chrome.tabs.create": {
        const requested = (params[0] as Record<string, unknown> | undefined) ?? {};
        if (requested.url && !/^https?:\/\//i.test(String(requested.url)) && requested.url !== "about:blank")
          throw new Error("Agent tabs require HTTP(S) or about:blank");
        // The server never chooses another local browser window.
        const tab = await this.api.tabs.create({ ...requested, windowId: this.windowId });
        if (tab.id === undefined || tab.windowId !== this.windowId)
          throw new Error("Browser did not create a tab in the side-panel window");
        return tab;
      }
      case "chrome.tabs.remove": {
        const values = Array.isArray(params[0]) ? params[0] : [params[0]];
        const tabIds = values.map(tabIdFromValue);
        for (const tabId of tabIds) {
          await this.requireTabInWindow(tabId);
          if (!this.attachedTabs.has(tabId))
            throw new Error("Playwright relay cannot close an uncontrolled tab");
        }
        await this.api.tabs.remove(tabIds.length === 1 ? tabIds[0] : tabIds);
        for (const tabId of tabIds) {
          this.forgetActivity(tabId);
          if (!this.attachedTabs.delete(tabId)) continue;
          this.notifyTabsChanged("tab_removed", tabId);
        }
        return {};
      }
      default:
        throw new Error(`Unsupported Playwright relay method: ${request.method}`);
    }
  }

  private notifyTabsChanged(
    reason: "attached" | "detached" | "tab_removed",
    tabId: number,
  ): void {
    this.onAttachedTabsChanged([...this.attachedTabs], reason, tabId);
  }
}

function tabIdFromValue(value: unknown): number {
  const tabId = Number(value);
  if (!Number.isInteger(tabId) || tabId < 0)
    throw new Error("Playwright relay command requires a valid tabId");
  return tabId;
}
