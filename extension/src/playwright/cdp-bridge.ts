/**
 * Copyright (c) Microsoft Corporation.
 * Copyright (c) Flowork contributors.
 *
 * Licensed under the Apache License, Version 2.0.
 */

import { PlaywrightBrowserModel, type CDPMessage } from "./browser-model";
import { NativeDownloads, type DownloadChrome } from "./native-downloads";
import {
  PlaywrightRelayExecutor,
  type PlaywrightRelayChrome,
  type PlaywrightRelayMessage,
  type RelayDebuggee,
  type RelayTab,
} from "./relay-executor";

export class PlaywrightCdpBridge {
  private readonly model: PlaywrightBrowserModel;
  private readonly relay: PlaywrightRelayExecutor;
  private relaySequence = 1;
  private readonly downloads?: NativeDownloads;

  constructor(
    api: PlaywrightRelayChrome,
    windowId: number,
    emitCDP: (message: CDPMessage) => void,
    onUnhandledError: (error: unknown) => void = console.error,
    onOwnedTabDetached: (tabId: number, reason: string) => void = () => undefined,
    onAttachedTabsChanged: (
      tabIds: number[],
      reason: "attached" | "detached" | "tab_removed",
      tabId: number,
    ) => void = () => undefined,
    private readonly cookieAccess?: (tabId: number, mode: "metadata" | "export" | "status", format: string) => Promise<unknown>,
    downloadChrome?: DownloadChrome,
    downloadConfirmationChanged: () => void = () => {},
    quotedTabWindows: ReadonlyMap<number, number> = new Map(),
  ) {
    this.model = new PlaywrightBrowserModel(
      async (method, params) => {
        const response = await this.relay.handle({
          id: this.relaySequence++,
          method,
          params,
        });
        if (response.error) throw new Error(response.error.message);
        return response.result;
      },
      onUnhandledError,
    );
    this.relay = new PlaywrightRelayExecutor(
      api,
      windowId,
      (message) => this.handleRelayEvent(message),
      onOwnedTabDetached,
      onAttachedTabsChanged,
      quotedTabWindows,
    );
    this.model.connectOverCDP(emitCDP);
    if (downloadChrome) this.downloads = new NativeDownloads(downloadChrome, windowId, tabId => this.ownsTab(tabId), undefined, downloadConfirmationChanged);
  }

  localDownloadConfirmations() { return this.downloads?.localConfirmations() ?? null; }
  async confirmLocalDownload(captureId: string, fileId: string | null): Promise<void> {
    if (!this.downloads) throw new Error("Download confirmation is no longer active");
    await this.downloads.confirmLocal(captureId, fileId);
  }

  initialize(tabs: RelayTab[]): void {
    for (const tab of tabs) this.model.onTabCreated(tab);
  }

  async handle(message: CDPMessage): Promise<CDPMessage> {
    if (!Number.isInteger(message.id))
      return { error: { code: -32600, message: "CDP command requires an integer id" } };
    const id = message.id;
    const method = String(message.method || "");
    try {
      let result: unknown;
      switch (method) {
        case "Flowork.downloadRead":
          throw new Error("download_approval_required: Native file reads require a trusted host envelope");
        case "Flowork.downloadBegin":
        case "Flowork.downloadInfo":
        case "Flowork.downloadEnd": {
          if (!this.downloads) throw new Error("Native downloads are not configured");
          const tabId = this.model.ownedTabId(message.sessionId);
          const params = (message.params ?? {}) as Record<string, unknown>;
          const captureId = String(params.capture_id || "");
          if (method === "Flowork.downloadBegin") result = await this.downloads.begin(tabId);
          else if (method === "Flowork.downloadInfo") result = { ...await this.downloads.info(tabId, captureId), tab_id: tabId };
          else { await this.downloads.end(tabId, captureId); result = { ended: true, capture_id: captureId }; }
          break;
        }
        case "Flowork.watchPopupDownloads":
        case "Flowork.popupDownloadCommand":
          throw new Error("Response-interception downloads have been retired. Use native Browser CLI downloads.");
        case "Flowork.cookieMetadata":
        case "Flowork.cookieExport":
        case "Flowork.cookieStatus": {
          if (!this.cookieAccess) throw new Error("Cookie access is not configured");
          const tabId = this.model.ownedTabId(message.sessionId);
          result = await this.cookieAccess(tabId, method === "Flowork.cookieExport" ? "export" : method === "Flowork.cookieStatus" ? "status" : "metadata",
            String((message.params as { format?: string })?.format || "json"));
          break;
        }
        case "Flowork.tabInfo":
          result = await this.model.tabInfo(message.sessionId);
          break;
        case "Browser.getVersion":
          result = {
            protocolVersion: "1.3",
            product: "Chrome/flowork-Extension-Bridge",
            userAgent: "flowork-Playwright-CDP-Bridge/1.0",
          };
          break;
        case "Browser.setDownloadBehavior":
          result = {};
          break;
        case "Target.setAutoAttach":
          if (message.sessionId)
            result = await this.model.sendCommand(
              message.sessionId,
              method,
              message.params ?? {},
            );
          else {
            await this.model.enableAutoAttach();
            result = {};
          }
          break;
        case "Target.createTarget":
          result = await this.model.createTarget(
            String((message.params as { url?: unknown } | undefined)?.url || "") || undefined,
          );
          break;
        case "Target.closeTarget":
          result = await this.model.closeTarget(
            String((message.params as { targetId?: unknown } | undefined)?.targetId || "") || undefined,
          );
          break;
        case "Target.getTargetInfo":
          result = { targetInfo: await this.model.getTargetInfo(message.sessionId,
            (message.params as { targetId?: unknown } | undefined)?.targetId) };
          break;
        default:
          result = message.sessionId
            ? await this.model.sendCommand(message.sessionId, method, message.params ?? {})
            : await this.model.sendBrowserCommand(method, message.params ?? {});
          break;
      }
      return { id, sessionId: message.sessionId, result: result ?? {} };
    } catch (error) {
      return {
        id,
        sessionId: message.sessionId,
        error: {
          code: -32603,
          message: error instanceof Error ? error.message : String(error),
        },
      };
    }
  }

  async close(): Promise<void> {
    this.model.disconnectFromCDP();
    await this.downloads?.close();
    await this.relay.close();
  }

  /** Host-only authenticated relay envelope; never a raw CDP method. */
  async authorizedDownloadRead(message: CDPMessage, grant: { tab_id: number; capture_id: string; candidate_id: string }): Promise<CDPMessage> {
    try {
      if (!this.downloads || this.model.ownedTabId(message.sessionId) !== grant.tab_id)
        throw new Error("The approved download belongs to a different tab");
      const params = (message.params ?? {}) as Record<string, unknown>;
      if (params.capture_id !== grant.capture_id || !Number.isSafeInteger(params.offset) || Number(params.offset) < 0)
        throw new Error("The approved download or chunk offset does not match");
      this.downloads.allowRead(grant.tab_id, grant.capture_id, grant.candidate_id);
      const result = await this.downloads.read(grant.tab_id, grant.capture_id, Number(params.offset));
      return { id: message.id, sessionId: message.sessionId, result };
    } catch (error) {
      return { id: message.id, sessionId: message.sessionId,
        error: { code: -32603, message: error instanceof Error ? error.message : String(error) } };
    }
  }

  attachedTabIds(): number[] {
    return this.relay.attachedTabIds();
  }

  ownsTab(tabId: number): boolean {
    return this.relay.attachedTabIds().includes(tabId);
  }

  notifyCookieRevoked(grantId: string): void {
    this.model.notifyCookieRevoked(grantId);
  }

  private handleRelayEvent(message: PlaywrightRelayMessage): void {
    const params = message.params ?? [];
    switch (message.method) {
      case "chrome.debugger.onEvent":
        this.model.onDebuggerEvent(
          params[0] as RelayDebuggee,
          String(params[1] || ""),
          (params[2] as Record<string, unknown> | undefined) ?? {},
        );
        break;
      case "chrome.debugger.onDetach":
        this.model.onDebuggerDetach(params[0] as RelayDebuggee);
        break;
      case "chrome.tabs.onCreated":
        this.model.onTabCreated(params[0] as RelayTab);
        break;
      case "chrome.tabs.onRemoved":
        this.model.onTabRemoved(Number(params[0]));
        break;
    }
  }
}
