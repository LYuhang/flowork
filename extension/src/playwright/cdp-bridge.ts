/**
 * Copyright (c) Microsoft Corporation.
 * Copyright (c) Flowork contributors.
 *
 * Licensed under the Apache License, Version 2.0.
 */

import { PlaywrightBrowserModel, type CDPMessage } from "./browser-model";
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
    );
    this.model.connectOverCDP(emitCDP);
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
          result = this.model.getTargetInfo(message.sessionId);
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
    await this.relay.close();
  }

  attachedTabIds(): number[] {
    return this.relay.attachedTabIds();
  }

  ownsTab(tabId: number): boolean {
    return this.relay.attachedTabIds().includes(tabId);
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
