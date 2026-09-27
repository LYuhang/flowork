/**
 * Copyright (c) Microsoft Corporation.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * Adapted from microsoft/playwright
 * packages/playwright-core/src/tools/mcp/browserModel.ts at commit
 * 680e5ad5894a54bba9e4ed8a311fd2aee388137d.
 *
 * Flowork adds scoped target/session authority, iframe routing and network
 * event ownership over its authenticated MV3 relay. Errors use an injected
 * callback instead of the upstream Node debug logger.
 */

import type { RelayDebuggee, RelayTab } from "./relay-executor";
import { redactNetworkEvent } from "./network-redaction";

export type CDPMessage = {
  id?: number;
  sessionId?: string;
  method?: string;
  params?: unknown;
  result?: unknown;
  error?: { code?: number; message: string };
};

export type SendRelayCommand = (method: string, params: unknown[]) => Promise<unknown>;
export type SendToCDPClient = (message: CDPMessage) => void;

type TabSession = {
  tabId: number;
  sessionId: string;
  targetInfo: Record<string, unknown> | undefined;
  childSessions: Set<string>;
};

export class PlaywrightBrowserModel {
  private sendToCDPClient: SendToCDPClient | null = null;
  private readonly knownTabs = new Map<number, RelayTab>();
  private readonly tabSessions = new Map<number, TabSession>();
  private autoAttach = false;
  private nextSessionId = 1;
  private readonly extraSessions = new Map<string, number>();
  private readonly extraSessionChildren = new Map<string, string>();
  private readonly childTargetInfo = new Map<string, Record<string, unknown>>();
  private readonly extraSessionParents = new Map<string, string | undefined>();
  private readonly browserSessions = new Set<string>();
  private readonly fetchOwners = new Map<string, string>();
  private readonly childFrameNavigations = new Map<string, Set<string>>();
  private readonly attachingTabs = new Map<number, Promise<TabSession>>();

  constructor(
    private readonly sendToExtension: SendRelayCommand,
    private readonly onUnhandledError: (error: unknown) => void = console.error,
  ) {}

  connectOverCDP(sendToCDPClient: SendToCDPClient): void {
    this.sendToCDPClient = sendToCDPClient;
  }

  disconnectFromCDP(): void {
    this.sendToCDPClient = null;
    this.autoAttach = false;
  }

  private emit(message: CDPMessage): void {
    this.sendToCDPClient?.(message);
  }

  onTabCreated(tab: RelayTab): void {
    if (tab.id === undefined) return;
    this.knownTabs.set(tab.id, tab);
    if (this.autoAttach)
      void this.attachTab(tab.id).catch(this.onUnhandledError);
  }

  onTabRemoved(tabId: number): void {
    this.knownTabs.delete(tabId);
    this.detachTab(tabId);
  }

  onDebuggerEvent(
    source: RelayDebuggee,
    method: string,
    params: Record<string, unknown> | undefined,
  ): void {
    if (source.tabId === undefined) return;
    const tabSession = this.tabSessions.get(source.tabId);
    if (!tabSession) return;
    params = redactNetworkEvent(method, params ?? {});
    // chrome.debugger has one physical session per tab. Logical Playwright
    // CDP sessions must not all receive Fetch pauses: the primary network
    // manager may continue a response before the download worker reads it.
    const fetchOwner = this.fetchOwners.get(`${source.tabId}:${source.sessionId || "main"}`);
    if (method.startsWith("Fetch.") && fetchOwner) {
      this.emit({ sessionId: fetchOwner, method, params: params ?? {} });
      return;
    }
    const childSessionId = String(params?.sessionId || "");
    if (method === "Target.attachedToTarget" && childSessionId) {
      tabSession.childSessions.add(childSessionId);
      this.childFrameNavigations.set(childSessionId, new Set());
      if (params?.targetInfo && typeof params.targetInfo === "object")
        this.childTargetInfo.set(childSessionId, params.targetInfo as Record<string, unknown>);
    } else if (method === "Target.detachedFromTarget" && childSessionId) {
      tabSession.childSessions.delete(childSessionId);
      this.childFrameNavigations.delete(childSessionId);
      this.childTargetInfo.delete(childSessionId);
      this.fetchOwners.delete(`${source.tabId}:${childSessionId}`);
      for (const [alias, child] of this.extraSessionChildren) {
        if (child === childSessionId) this.forgetExtraSession(alias);
      }
    }
    if (source.sessionId && method === "Page.frameNavigated") {
      const frame = params?.frame as { id?: string } | undefined;
      if (frame?.id) this.childFrameNavigations.get(source.sessionId)?.add(frame.id);
    }
    this.emit({
      sessionId: source.sessionId || tabSession.sessionId,
      method,
      params: params ?? {},
    });
    for (const [sessionId, tabId] of this.extraSessions) {
      if (tabId === source.tabId && this.extraSessionChildren.get(sessionId) === source.sessionId)
        this.emit({ sessionId, method, params: params ?? {} });
    }
  }

  onDebuggerDetach(source: RelayDebuggee): void {
    if (source.tabId !== undefined) this.detachTab(source.tabId);
  }

  async enableAutoAttach(): Promise<void> {
    this.autoAttach = true;
    await Promise.all(
      [...this.knownTabs.keys()].map((tabId) =>
        this.attachTab(tabId).catch((error) => {
          this.onUnhandledError(error);
          return undefined;
        }),
      ),
    );
  }

  async createTarget(url: string | undefined): Promise<{ targetId: string | undefined }> {
    const tab = (await this.sendToExtension("chrome.tabs.create", [{ url }])) as RelayTab;
    if (tab?.id === undefined) throw new Error("Failed to create tab");
    this.knownTabs.set(tab.id, tab);
    const tabSession = await this.attachTab(tab.id);
    return { targetId: String(tabSession.targetInfo?.targetId || "") || undefined };
  }

  async closeTarget(targetId: string | undefined): Promise<{ success: boolean }> {
    const tabSession = targetId
      ? this.findTabSession((session) => session.targetInfo?.targetId === targetId)
      : undefined;
    if (!tabSession) return { success: false };
    await this.sendToExtension("chrome.tabs.remove", [tabSession.tabId]);
    return { success: true };
  }

  async getTargetInfo(sessionId: string | undefined, targetId?: unknown): Promise<Record<string, unknown>> {
    const child = sessionId ? this.extraSessionChildren.get(sessionId) || sessionId : undefined;
    const callerInfo = () => {
      const callerChild = sessionId ? this.extraSessionChildren.get(sessionId) || sessionId : undefined;
      return callerChild && this.childTargetInfo.has(callerChild) ? this.childTargetInfo.get(callerChild)
        : this.findTabSession(session => session.sessionId === sessionId ||
          (sessionId !== undefined && session.tabId === this.extraSessions.get(sessionId)))?.targetInfo;
    };
    const current = callerInfo();
    if (sessionId && !this.browserSessions.has(sessionId) && !current)
      throw new Error("No authorized target for this session");
    const refresh = async (session: TabSession, childId?: string): Promise<Record<string, unknown>> => {
      const expected = childId ? this.childTargetInfo.get(childId) : session.targetInfo;
      const result = await this.sendToExtension("chrome.debugger.sendCommand", [
        { tabId: session.tabId, ...(childId ? { sessionId: childId } : {}) },
        "Target.getTargetInfo", {},
      ]) as { targetInfo?: Record<string, unknown> };
      if (this.tabSessions.get(session.tabId) !== session || (childId && !session.childSessions.has(childId)) ||
          (sessionId && !this.browserSessions.has(sessionId) && !callerInfo()))
        throw new Error("Target authorization changed during inspection");
      if (!expected?.targetId || result.targetInfo?.targetId !== expected.targetId)
        throw new Error("Target metadata changed during inspection");
      if (childId) this.childTargetInfo.set(childId, result.targetInfo);
      else session.targetInfo = result.targetInfo;
      return result.targetInfo;
    };
    if (targetId !== undefined) {
      if (typeof targetId !== "string" || !targetId)
        throw new Error("Target.getTargetInfo requires a nonempty targetId when provided");
      // Resolve only from live scoped metadata. Never silently substitute the
      // current page or forward a foreign target selector to native Chrome.
      for (const session of this.tabSessions.values()) {
        if (session.targetInfo?.targetId === targetId) return refresh(session);
        for (const id of session.childSessions) {
          const info = this.childTargetInfo.get(id);
          if (info?.type === "iframe" && info.targetId === targetId) return refresh(session, id);
        }
      }
      throw new Error("Cannot inspect an unauthorized target");
    }
    // Playwright uses an unqualified root query as an initialization barrier.
    // Describe our synthetic scoped browser, never Chrome's global target.
    if (!current) return { targetId: "flowork-scoped-browser", type: "browser",
      title: "Flowork scoped browser", url: "", attached: true };
    const owner = this.findTabSession(session => session.targetInfo === current || !!child && session.childSessions.has(child));
    if (!owner) throw new Error("No authorized target for this session");
    return refresh(owner, child && owner.childSessions.has(child) ? child : undefined);
  }

  async tabInfo(sessionId: string | undefined): Promise<unknown> {
    const session = this.findTabSession(value => value.sessionId === sessionId || value.tabId === this.extraSessions.get(sessionId || ""));
    if (!session) throw new Error("No authorized tab for this session");
    const tab = await this.sendToExtension("chrome.tabs.get", [{ tabId: session.tabId }]) as RelayTab;
    return { target_id: session.targetInfo?.targetId, window_id: `win_${tab.windowId}`, active: !!tab.active,
      title: tab.title || "", url: tab.url || "", capabilities: { popup_downloads: true } };
  }

  ownedTabId(sessionId: string | undefined): number {
    const session = this.findTabSession(value => value.sessionId === sessionId || value.tabId === this.extraSessions.get(sessionId || ""));
    if (!session) throw new Error("No authorized tab for this session");
    return session.tabId;
  }

  notifyCookieRevoked(grantId: string): void {
    for (const session of this.tabSessions.values())
      this.emit({ sessionId: session.sessionId, method: "Flowork.cookieConsentRevoked", params: { grant_id: grantId } });
    for (const sessionId of this.extraSessions.keys())
      this.emit({ sessionId, method: "Flowork.cookieConsentRevoked", params: { grant_id: grantId } });
  }

  attachOwnedTarget(targetId: string, parentSessionId?: string): { sessionId: string } {
    let session = this.findTabSession(value => value.targetInfo?.targetId === targetId);
    let child: string | undefined;
    if (!session) {
      session = this.findTabSession(value => {
        child = [...value.childSessions].find(id => {
          const info = this.childTargetInfo.get(id);
          return info?.type === "iframe" && info.targetId === targetId;
        });
        return !!child;
      });
    }
    if (!session) throw new Error("Cannot attach to an unauthorized target");
    const sessionId = `pw-extra-${this.nextSessionId++}`;
    this.extraSessions.set(sessionId, session.tabId);
    if (child) this.extraSessionChildren.set(sessionId, child);
    this.extraSessionParents.set(sessionId, parentSessionId);
    this.emit({ ...(parentSessionId ? { sessionId: parentSessionId } : {}), method: "Target.attachedToTarget", params: {
      sessionId, targetInfo: { ...(child ? this.childTargetInfo.get(child) : session.targetInfo), attached: true }, waitingForDebugger: false,
    } });
    return { sessionId };
  }

  async detachExtraSession(sessionId: string): Promise<void> {
    const tabId = this.extraSessions.get(sessionId);
    if (tabId === undefined) throw new Error("Cannot detach an unowned CDP session");
    if (this.fetchOwners.get(`${tabId}:${this.extraSessionChildren.get(sessionId) || "main"}`) === sessionId)
      await this.sendCommand(sessionId, "Fetch.disable", {});
    this.forgetExtraSession(sessionId);
  }

  private forgetExtraSession(sessionId: string): void {
    this.extraSessions.delete(sessionId);
    this.extraSessionChildren.delete(sessionId);
    const parentSessionId = this.extraSessionParents.get(sessionId);
    this.extraSessionParents.delete(sessionId);
    this.emit({ ...(parentSessionId ? { sessionId: parentSessionId } : {}), method: "Target.detachedFromTarget", params: { sessionId } });
  }

  async sendBrowserCommand(method: string, params: unknown, parentSessionId?: string): Promise<unknown> {
    // Playwright newCDPSession first requests a browser-target session. This is
    // a synthetic routing alias, not access to Chrome's global debugger target.
    // All requests on it still use this exact scoped command allow-list.
    if (method === "Target.attachToBrowserTarget") {
      const sessionId = `pw-browser-${this.nextSessionId++}`;
      this.browserSessions.add(sessionId);
      return { sessionId };
    }
    if (method === "Target.getTargets") return { targetInfos: [...this.tabSessions.values()].map(value => value.targetInfo) };
    if (method === "Target.getBrowserContexts") return { browserContextIds: [] };
    if (method === "Target.attachToTarget") return this.attachOwnedTarget(String((params as { targetId?: string })?.targetId || ""), parentSessionId);
    if (method === "Target.detachFromTarget") {
      const sessionId = String((params as { sessionId?: string })?.sessionId || "");
      if (this.browserSessions.has(sessionId)) {
        for (const [child, parent] of this.extraSessionParents)
          if (parent === sessionId) await this.detachExtraSession(child);
        this.browserSessions.delete(sessionId);
        this.emit({ method: "Target.detachedFromTarget", params: { sessionId } });
      } else await this.detachExtraSession(sessionId);
      return {};
    }
    throw new Error(`Browser-wide CDP command is not permitted: ${method}`);
  }

  async sendCommand(sessionId: string, method: string, params: unknown): Promise<unknown> {
    if (this.browserSessions.has(sessionId)) return this.sendBrowserCommand(method, params, sessionId);
    let tabSession = this.findTabSession((session) => session.sessionId === sessionId || session.tabId === this.extraSessions.get(sessionId));
    let childSessionId = this.extraSessionChildren.get(sessionId);
    if (!tabSession) {
      tabSession = this.findTabSession((session) => session.childSessions.has(sessionId));
      childSessionId = sessionId;
    }
    if (!tabSession) throw new Error(`No tab found for sessionId: ${sessionId}`);
    if (method === "Target.detachFromTarget") {
      const child = String((params as { sessionId?: string })?.sessionId || "");
      if (!tabSession.childSessions.has(child))
        throw new Error("Cannot detach an unauthorized child session");
      // Do not forward targetId or other selectors supplied by a raw client.
      params = { sessionId: child };
    }
    const fetchKey = `${tabSession.tabId}:${childSessionId || "main"}`;
    const owner = this.fetchOwners.get(fetchKey);
    if (method.startsWith("Fetch.") && owner && owner !== sessionId)
      throw new Error("Request interception belongs to another CDP session on this tab");
    if (method === "Fetch.enable") this.fetchOwners.set(fetchKey, sessionId);
    try {
      const result = await this.sendToExtension("chrome.debugger.sendCommand", [
        { tabId: tabSession.tabId, sessionId: childSessionId },
        method,
        params,
      ]);
      // An extension commonly attaches to an already-loaded OOPIF. Chrome
      // does not replay its committed navigation on Page.enable, and
      // Playwright only hydrates the main target from Page.getFrameTree.
      // Seed that child session from Chrome's actual frame metadata once.
      // Never replace a live navigation that raced with the tree request.
      if (childSessionId && method === "Page.getFrameTree") {
        const frame = (result as { frameTree?: { frame?: { id?: string } } })?.frameTree?.frame;
        const navigations = this.childFrameNavigations.get(childSessionId);
        if (frame?.id && navigations && !navigations.has(frame.id)) {
          navigations.add(frame.id);
          this.emit({ sessionId: childSessionId, method: "Page.frameNavigated", params: { frame, type: "Navigation" } });
        }
      }
      if (method === "Fetch.disable") this.fetchOwners.delete(fetchKey);
      return result;
    } catch (error) {
      if (method === "Fetch.enable") {
        if (owner) this.fetchOwners.set(fetchKey, owner);
        else this.fetchOwners.delete(fetchKey);
      }
      throw error;
    }
  }

  private async attachTab(tabId: number): Promise<TabSession> {
    const existing = this.tabSessions.get(tabId);
    if (existing) return existing;
    const pending = this.attachingTabs.get(tabId);
    if (pending) return pending;
    const starting = this.attachNewTab(tabId);
    this.attachingTabs.set(tabId, starting);
    try { return await starting; }
    finally { this.attachingTabs.delete(tabId); }
  }

  private async attachNewTab(tabId: number): Promise<TabSession> {
    await this.sendToExtension("chrome.debugger.attach", [
      { tabId }, "1.3",
    ]);
    const result = (await this.sendToExtension("chrome.debugger.sendCommand", [
      { tabId },
      "Target.getTargetInfo",
      {},
    ])) as { targetInfo?: Record<string, unknown> } | undefined;
    const targetInfo = result?.targetInfo;
    const sessionId = `pw-tab-${this.nextSessionId++}`;
    const tabSession: TabSession = {
      tabId,
      sessionId,
      targetInfo,
      childSessions: new Set(),
    };
    this.tabSessions.set(tabId, tabSession);
    this.emit({
      method: "Target.attachedToTarget",
      params: {
        sessionId,
        targetInfo: { ...targetInfo, attached: true },
        waitingForDebugger: false,
      },
    });
    return tabSession;
  }

  private detachTab(tabId: number): void {
    const tabSession = this.tabSessions.get(tabId);
    if (!tabSession) return;
    this.tabSessions.delete(tabId);
    for (const child of tabSession.childSessions) {
      this.childFrameNavigations.delete(child);
      this.childTargetInfo.delete(child);
    }
    for (const key of this.fetchOwners.keys()) {
      if (key.startsWith(`${tabId}:`)) this.fetchOwners.delete(key);
    }
    for (const [sessionId, owner] of this.extraSessions) {
      if (owner === tabId) {
        this.forgetExtraSession(sessionId);
      }
    }
    this.emit({
      method: "Target.detachedFromTarget",
      params: {
        sessionId: tabSession.sessionId,
        targetId: tabSession.targetInfo?.targetId,
      },
    });
  }

  private findTabSession(
    predicate: (session: TabSession) => boolean,
  ): TabSession | undefined {
    for (const session of this.tabSessions.values()) {
      if (predicate(session)) return session;
    }
    return undefined;
  }
}
