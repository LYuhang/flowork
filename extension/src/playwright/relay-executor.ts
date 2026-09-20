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
 * invokes a five-command chrome.* allow-list and forwards events for tabs that
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
  active?: boolean;
};

export type RelayDebuggee = { tabId?: number; sessionId?: string };

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
  };
};

export const PLAYWRIGHT_RELAY_ALLOWED_COMMANDS = new Set([
  "chrome.debugger.attach",
  "chrome.debugger.detach",
  "chrome.debugger.sendCommand",
  "chrome.tabs.create",
  "chrome.tabs.remove",
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
  private readonly removeListeners: Array<() => void> = [];
  private closed = false;

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
      if (source.tabId === undefined || !this.attachedTabs.has(source.tabId)) return;
      this.emit({
        method: "chrome.debugger.onEvent",
        params: [source, method, params ?? {}],
      });
    };
    const onDebuggerDetach = (source: RelayDebuggee, reason: string) => {
      if (source.tabId === undefined || !this.attachedTabs.has(source.tabId)) return;
      this.attachedTabs.delete(source.tabId);
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
      this.emit({ method: "chrome.tabs.onCreated", params: [tab] });
    };
    const onTabRemoved = (tabId: number) => {
      if (!this.attachedTabs.has(tabId)) return;
      this.attachedTabs.delete(tabId);
      this.emit({ method: "chrome.tabs.onRemoved", params: [tabId] });
      this.notifyTabsChanged("tab_removed", tabId);
    };

    api.debugger.onEvent.addListener(onDebuggerEvent);
    api.debugger.onDetach.addListener(onDebuggerDetach);
    api.tabs.onCreated.addListener(onTabCreated);
    api.tabs.onRemoved.addListener(onTabRemoved);
    this.removeListeners.push(
      () => api.debugger.onEvent.removeListener(onDebuggerEvent),
      () => api.debugger.onDetach.removeListener(onDebuggerDetach),
      () => api.tabs.onCreated.removeListener(onTabCreated),
      () => api.tabs.onRemoved.removeListener(onTabRemoved),
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
    if (this.closed) return;
    this.closed = true;
    for (const remove of this.removeListeners.splice(0)) remove();
    const tabs = [...this.attachedTabs];
    this.attachedTabs.clear();
    await Promise.allSettled(
      tabs.map((tabId) => this.api.debugger.detach({ tabId })),
    );
  }

  attachedTabIds(): number[] {
    return [...this.attachedTabs];
  }

  private async requireTabInWindow(tabId: number): Promise<RelayTab> {
    const tab = await this.api.tabs.get(tabId);
    if (tab.windowId !== this.windowId)
      throw new Error("Playwright relay target is outside the side-panel window");
    return tab;
  }

  private async invoke(request: PlaywrightRelayRequest): Promise<unknown> {
    const params = request.params ?? [];
    switch (request.method) {
      case "chrome.debugger.attach": {
        const target = params[0] as RelayDebuggee;
        const tabId = tabIdFrom(target);
        await this.requireTabInWindow(tabId);
        await this.api.debugger.attach({ tabId }, String(params[1] || "1.3"));
        this.attachedTabs.add(tabId);
        this.notifyTabsChanged("attached", tabId);
        return {};
      }
      case "chrome.debugger.detach": {
        const tabId = tabIdFrom(params[0]);
        if (!this.attachedTabs.has(tabId))
          throw new Error("Playwright relay cannot detach an uncontrolled tab");
        await this.api.debugger.detach({ tabId });
        if (this.attachedTabs.delete(tabId))
          this.notifyTabsChanged("detached", tabId);
        return {};
      }
      case "chrome.debugger.sendCommand": {
        const tabId = tabIdFrom(params[0]);
        if (!this.attachedTabs.has(tabId))
          throw new Error("Playwright relay cannot address an uncontrolled tab");
        const source = params[0] as RelayDebuggee;
        return (
          (await this.api.debugger.sendCommand(
            source.sessionId ? { tabId, sessionId: source.sessionId } : { tabId },
            String(params[1] || ""),
            (params[2] as Record<string, unknown> | undefined) ?? {},
          )) ?? {}
        );
      }
      case "chrome.tabs.create": {
        const requested = (params[0] as Record<string, unknown> | undefined) ?? {};
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
