/**
 * MV3 background service worker.
 *
 * Receives the web app's AUTH_SYNC over `externally_connectable`
 * (auth-only account sharing — no chat handoff), and relay the side-panel
 * embed's OPEN_WS to the offscreen document (which holds the socket, B1).
 *
 * Playwright CDP relay execution lives here because `chrome.debugger` is
 * available in the service worker but not in offscreen documents. The worker
 * also projects lifecycle state to the Dynamic Island and backend WebSocket.
 *
 * Robustness note (§23): the SW is disposable. Session identity and controlled
 * tab ids live in storage.session; a replacement worker adopts debugger targets
 * that are still attached, or re-attaches tabs whose debugger was detached.
 */
import { encode } from "./shared/envelope";
import { isAllowedWebAppSenderUrl, WS_BASE } from "./shared/config";
import {
  type PlaywrightRelayChrome,
  type RelayTab,
} from "./playwright/relay-executor";
import {
  PlaywrightCdpBridge,
} from "./playwright/cdp-bridge";
import type { CDPMessage } from "./playwright/browser-model";

// ---- CDP layer (service-worker context — chrome.debugger lives here) ----

const playwrightRelayChrome: PlaywrightRelayChrome = {
  debugger: {
    attach: (target, version) => chrome.debugger.attach(target, version),
    detach: (target) => chrome.debugger.detach(target),
    sendCommand: (target, method, params) =>
      chrome.debugger.sendCommand(target, method, params),
    onEvent: chrome.debugger.onEvent,
    onDetach: chrome.debugger.onDetach,
  },
  tabs: {
    get: (tabId) => chrome.tabs.get(tabId) as Promise<RelayTab>,
    create: (properties) =>
      chrome.tabs.create(properties as chrome.tabs.CreateProperties) as Promise<RelayTab>,
    remove: (tabIds) =>
      Array.isArray(tabIds)
        ? chrome.tabs.remove(tabIds)
        : chrome.tabs.remove(tabIds),
    onCreated: chrome.tabs.onCreated as unknown as PlaywrightRelayChrome["tabs"]["onCreated"],
    onRemoved: chrome.tabs.onRemoved,
  },
};

let playwrightCdpBridge: PlaywrightCdpBridge | null = null;

async function closePlaywrightCdpBridge(): Promise<void> {
  const bridge = playwrightCdpBridge;
  if (!bridge) return;
  playwrightCdpBridge = null;
  await bridge.close();
}

// The window the side panel is docked in (reported by sidepanel.ts on mount /
// focus). When we adopt "the user's current tab" we scope the active-tab query to
// THIS window — otherwise `currentWindow` from the (windowless) service worker
// resolves to the last-focused window, which can be a DIFFERENT browser window
// than the one the user opened the side panel in. Mirrored in storage.session so
// it survives a SW restart.
let activePanelWindowId: number | undefined;
let activePanelContextId: string | undefined;
let browserSessionEventSeq = 0;
type BrowserSessionState = {
  sessionId?: string;
  channel?: string;
  transport?: string;
  chatId?: string;
  browserWindowId?: string;
  panelContextId?: string;
  sessionGeneration?: number;
};
let currentBrowserSession: BrowserSessionState | null = null;

// MV3 service workers can restart while the offscreen WebSocket and debugger
// session continue. Session identity and the event sequence are therefore
// durable extension state, not process-local variables.
const browserSessionStateReady = chrome.storage.session
  .get([
    "browserSessionEventSeq",
    "currentBrowserSession",
    "activePanelWindowId",
    "activePanelContextId",
  ])
  .then((stored) => {
    const seq = Number(stored.browserSessionEventSeq || 0);
    browserSessionEventSeq = Number.isFinite(seq) && seq >= 0 ? seq : 0;
    const session = stored.currentBrowserSession;
    if (session && typeof session === "object") {
      currentBrowserSession = session as BrowserSessionState;
    }
    if (typeof stored.activePanelWindowId === "number") {
      activePanelWindowId = stored.activePanelWindowId;
    }
    if (typeof stored.activePanelContextId === "string") {
      activePanelContextId = stored.activePanelContextId;
    }
  })
  .catch(() => undefined);

async function setCurrentBrowserSession(
  session: BrowserSessionState | null,
): Promise<void> {
  await browserSessionStateReady;
  currentBrowserSession = session;
  if (session) {
    await chrome.storage.session.set({ currentBrowserSession: session });
    // A newly reserved generation proves any older terminal event has already
    // been reconciled by the backend; do not replay it into the new session.
    await chrome.storage.session.remove("pendingBrowserSessionTerminalEvent");
  } else {
    await chrome.storage.session.remove("currentBrowserSession");
  }
}

async function rememberedControlledTabIds(): Promise<number[]> {
  const stored = await chrome.storage.session.get([
    "controlledTabId",
    "controlledTabIds",
  ]);
  const ids = Array.isArray(stored.controlledTabIds)
    ? stored.controlledTabIds.filter(
        (value: unknown): value is number =>
          typeof value === "number" && Number.isInteger(value) && value >= 0,
      )
    : [];
  if (typeof stored.controlledTabId === "number") ids.push(stored.controlledTabId);
  return [...new Set(ids)];
}

async function liveRememberedControlledTabIds(): Promise<number[]> {
  const remembered = await rememberedControlledTabIds();
  const live: number[] = [];
  for (const tabId of remembered) {
    try {
      const tab = await chrome.tabs.get(tabId);
      const expectedWindow = Number(
        currentBrowserSession?.browserWindowId ?? activePanelWindowId,
      );
      if (
        Number.isInteger(expectedWindow) &&
        tab.windowId === expectedWindow &&
        !!tab.url &&
        /^(https?|file):/.test(tab.url)
      ) {
        live.push(tabId);
      }
    } catch {
      // A closed or inaccessible tab is not part of the live control lease.
    }
  }
  if (live.length !== remembered.length) {
    if (live.length) {
      await chrome.storage.session.set({
        controlledTabId: live[0],
        controlledTabIds: live,
      });
    } else {
      await chrome.storage.session.remove(["controlledTabId", "controlledTabIds"]);
    }
  }
  return live;
}

async function handlePlaywrightTabRemoved(tabId: number): Promise<void> {
  const remembered = await rememberedControlledTabIds();
  if (!remembered.includes(tabId)) return;
  const remaining = await liveRememberedControlledTabIds();
  if (remaining.length > 0) {
    await chrome.storage.session.set({
      controlledTabId: remaining[0],
      controlledTabIds: remaining,
    });
    void setIsland(true, "ready");
    return;
  }
  await releaseControlledBrowserSession("last_tab_closed", { tabId });
}

chrome.tabs.onRemoved.addListener((tabId) => {
  // Let Playwright consume the native event and update its target model first.
  setTimeout(() => void handlePlaywrightTabRemoved(tabId), 0);
});

function handlePlaywrightDebuggerDetach(tabId: number, reason: string): void {
  if (sessionReleaseInProgress) return;
  // The bridge cannot safely keep driving a page after Chrome or the user has
  // removed its debugger attachment. Release the fenced session; a later user
  // message can initialize a fresh Playwright connection to the visible page.
  setTimeout(() => {
    void releaseControlledBrowserSession(
      String(reason || "debugger_detached"),
      { tabId },
    );
  }, 0);
}

async function persistPlaywrightControlledTabs(ids: number[]): Promise<void> {
  ids = [...new Set(ids)];
  if (ids.length === 0) {
    await chrome.storage.session.remove(["controlledTabId", "controlledTabIds"]);
    return;
  }
  await chrome.storage.session.set({
    controlledTabId: ids[0],
    controlledTabIds: ids,
  });
}

function chatIdFromChannel(channel?: string): string {
  return channel?.startsWith("chat:") ? channel.slice("chat:".length) : "";
}

let browserSessionEventQueue: Promise<void> = Promise.resolve();

function broadcastBrowserSessionChanged(payload: Record<string, unknown>): Promise<void> {
  const requestedSession = currentBrowserSession
    ? { ...currentBrowserSession }
    : null;
  const task = browserSessionEventQueue.then(async () => {
    await browserSessionStateReady;
    const session = requestedSession || currentBrowserSession;
    const seq = ++browserSessionEventSeq;
    await chrome.storage.session.set({ browserSessionEventSeq: seq });
  const enriched = {
    ...payload,
    session_id:
      typeof payload.session_id === "string"
        ? payload.session_id
        : session?.sessionId || "",
    browser_session_id:
      typeof payload.browser_session_id === "string"
        ? payload.browser_session_id
        : session?.sessionId || "",
    session_generation:
      typeof payload.session_generation === "number"
        ? payload.session_generation
        : session?.sessionGeneration || 0,
    chat_id:
      typeof payload.chat_id === "string"
        ? payload.chat_id
        : session?.chatId || chatIdFromChannel(session?.channel),
    window_id:
      payload.window_id ?? session?.browserWindowId ?? "",
    browser_window_id:
      payload.browser_window_id ?? session?.browserWindowId ?? "",
    panel_context_id:
      payload.panel_context_id ?? session?.panelContextId ?? "",
    event_seq: seq,
  };
  const status = String(payload.status || "");
  if (status === "released" || status === "inactive") {
    // Keep the terminal event until the iframe acknowledges its fenced HTTP
    // write. If the WS was offline and no panel was open, replay it on reconnect.
    await chrome.storage.session.set({
      pendingBrowserSessionTerminalEvent: enriched,
    });
  }
  void chrome.runtime.sendMessage({
    type: "BROWSER_SESSION_CHANGED",
    ...enriched,
  }).catch(() => {
    // No side panel currently open; the backend event below is still attempted.
  });
  try {
    const backendEvent: Record<string, unknown> = { ...enriched };
    delete backendEvent.window_id;
    delete backendEvent.browser_window_id;
    delete backendEvent.panel_context_id;
    await chrome.runtime.sendMessage({
      type: "WS_SEND",
      raw: encode("event", {
        id: `evt_browser_session_${Date.now()}_${seq}`,
        channel: session?.channel || "system",
        transport: session?.transport || "pending",
        data: {
          type: "browser_session_changed",
          ...backendEvent,
        },
      }),
    });
  } catch {
    // WS may be down; the iframe release fallback and command errors still apply.
  }
  if (status === "released" || status === "inactive") {
      await setCurrentBrowserSession(null);
  }
  });
  browserSessionEventQueue = task.catch(() => undefined);
  return task;
}

function sendBrowserSessionSnapshot(): Promise<void> {
  const task = browserSessionEventQueue.then(async () => {
    await browserSessionStateReady;
    const session = currentBrowserSession;
    if (!session?.sessionId || !session.chatId || !session.sessionGeneration) return;
    const tabIds = await liveRememberedControlledTabIds();
    const seq = ++browserSessionEventSeq;
    await chrome.storage.session.set({ browserSessionEventSeq: seq });
    await chrome.runtime.sendMessage({
      type: "WS_SEND",
      raw: encode("event", {
        id: `evt_browser_snapshot_${Date.now()}_${seq}`,
        channel: session.channel || `chat:${session.chatId}`,
        transport: session.transport || "pending",
        data: {
          type: "browser_session_snapshot",
          chat_id: session.chatId,
          browser_session_id: session.sessionId,
          session_generation: session.sessionGeneration,
          event_seq: seq,
          controlled: tabIds.length > 0,
          tab_ids: tabIds,
          reason: "websocket_reconnected",
        },
      }),
    });
  });
  browserSessionEventQueue = task.catch(() => undefined);
  return task;
}

async function replayPendingBrowserTerminalEvent(): Promise<void> {
  const stored = await chrome.storage.session.get("pendingBrowserSessionTerminalEvent");
  const event = stored.pendingBrowserSessionTerminalEvent;
  if (!event || typeof event !== "object") return;
  const record = event as Record<string, unknown>;
  const chatId = typeof record.chat_id === "string" ? record.chat_id : "";
  if (!chatId) return;
  const backendEvent = { ...record };
  delete backendEvent.window_id;
  delete backendEvent.browser_window_id;
  delete backendEvent.panel_context_id;
  await chrome.runtime.sendMessage({
    type: "WS_SEND",
    raw: encode("event", {
      id: `evt_browser_terminal_replay_${Date.now()}`,
      channel: `chat:${chatId}`,
      transport: "reconnect",
      data: {
        type: "browser_session_changed",
        ...backendEvent,
      },
    }),
  });
}

async function releaseControlledBrowserSession(
  reason: string,
  extra: Record<string, unknown> = {},
): Promise<void> {
  if (sessionReleaseInProgress) return;
  sessionReleaseInProgress = true;
  try {
    await browserSessionStateReady;
    const tabs = [
      ...new Set([
        ...(playwrightCdpBridge?.attachedTabIds() ?? []),
        ...(await rememberedControlledTabIds()),
      ]),
    ];
    await closePlaywrightCdpBridge();
    await chrome.storage.session
      .remove(["controlledTabId", "controlledTabIds"])
      .catch(() => {});
    void setIsland(false);
    await broadcastBrowserSessionChanged({
      status: "released",
      reason,
      tab_ids: tabs,
      ...extra,
    });
  } finally {
    sessionReleaseInProgress = false;
  }
}

// Keep-alive: the persistent offscreen holds a long-lived port to us. While that
// port is connected the SW is NOT evicted — so the chrome.debugger session it
// holds isn't released (which would drop the "debugging" banner + the attach,
// forcing a re-attach after every command). §23 robustness.
chrome.runtime.onConnect.addListener((port) => {
  if (port.name === "keepalive") {
    // Each inbound ping resets the SW idle timer (the actual keep-alive).
    port.onMessage.addListener(() => void 0);
    port.onDisconnect.addListener(() => void 0);
  }
});

// Clicking the toolbar icon opens the side panel DIRECTLY (no popup / no extra
// "open side panel" click). Requires an `action` in the manifest. Idempotent —
// safe to call on every SW spawn; wrapped so an old Chrome without the API can't
// break startup.
try {
  void chrome.sidePanel
    ?.setPanelBehavior?.({ openPanelOnActionClick: true })
    .catch((e) => console.warn("[flowork] setPanelBehavior failed", e));
} catch {
  /* sidePanel API unavailable — non-fatal */
}

let sessionReleaseInProgress = false;

// ---- Authentication handshake ----

/**
 * Ensure exactly one offscreen document exists before we forward `OPEN_WS`.
 * Reason: Chrome's `offscreen` API has no "WebSocket" reason; `WEB_RTC` is the
 * closest valid member for a long-lived socket that must outlive the SW.
 */
async function ensureOffscreen(): Promise<void> {
  const has = await chrome.offscreen.hasDocument();
  if (has) return;
  await chrome.offscreen.createDocument({
    url: "offscreen.html",
    reasons: [chrome.offscreen.Reason.WEB_RTC, chrome.offscreen.Reason.CLIPBOARD],
    justification:
      "Hold the tenant-scoped backend WebSocket and stage write-only native rich-text paste.",
  });
}

interface AuthSyncMsg {
  type: "AUTH_SYNC" | "AUTH_CLEAR";
  /** Single-use code redeemed by the iframe for a partitioned HttpOnly Session.
   * The primary Web Session is never stored by the extension. */
  exchangeCode: string;
  tenant?: string;
  /** The main app's resolved model settings (credential_id, temperature,
   *  max_tokens, timeout), relayed so the embedded chat runs with the same
   *  credential and generation parameters. Stored under
   *  `embedAgentSettings` and echoed back via GET_BINDING / BINDING. */
  agentSettings?: Record<string, unknown>;
}

// One-way migration from the pre-cookie implementation. A primary Web
// Session must never survive in extension storage after this worker starts.
void chrome.storage.local.remove("embedSessionToken");

/**
 * The browser-id is STABLE per browser profile (persisted), not per handoff:
 * it identifies the WS connection / this browser, so the backend can route a
 * chat turn's browser tools to this exact transport across reconnects and new
 * handoffs. Generate once with crypto.randomUUID and reuse forever.
 */
async function getStableBrowserId(): Promise<string> {
  const { browserId } = await chrome.storage.local.get("browserId");
  if (typeof browserId === "string" && browserId) return browserId;
  const id = crypto.randomUUID();
  await chrome.storage.local.set({ browserId: id });
  return id;
}

/** Minimal SW-side view of the connection, surfaced to the side panel. */
const state: { connected: boolean; wfId: string } = {
  connected: false,
  wfId: "",
};

/**
 * Last binding pushed to the side-panel iframe: the embedded chat is
 * linked to the controlled browser by `(wf_id, chat_id, browser_id)`. browser_id
 * is the stable per-browser id; chat_id may be empty until the embed creates one.
 */
const binding: {
  wf_id: string;
  chat_id: string;
  browser_id: string;
  /** App Relay — the in-flight instruction relayed from the main app
   *  for the embed to auto-send once. Empty when not relayed (entry B). */
  instruction: string;
  /** The web app's full origin+path-prefix base (from the handoff), so the
   *  side panel builds the /embed/chat URL with the proxy prefix. "" → fall back
   *  to the bundled WEB_BASE. */
  webBase: string;
  /** The WS base (ws(s)://host[/prefix]) from the handoff. The embed's own
   *  OPEN_WS relay doesn't carry it, so we fall back to THIS stored value —
   *  otherwise the offscreen builds a relative URL and WebSocket throws
   *  "scheme … chrome-extension … not allowed". */
  wsBase: string;
} = {
  wf_id: "",
  chat_id: "",
  browser_id: "",
  instruction: "",
  webBase: "",
  wsBase: "",
};

// NOTE: webBase/wsBase no longer come from a runtime handoff — they are BAKED
// into the bundle (WEB_BASE / WS_BASE in shared/config), set per-environment at
// build time. So the binding no longer needs to survive SW eviction; the side
// panel builds its /embed/chat URL from the baked WEB_BASE and the offscreen
// reconnects with the baked WS_BASE. Auth is synced separately (AUTH_SYNC).

/**
 * Best-effort write of the Dynamic Island state the content script subscribes to
 * (`chrome.storage.session.islandState`). Schema:
 *   { controlled, kind: "ready"|"thinking"|"tool"|"browser_tool"|"streaming"|"confirm", tool? }
 *
 * VISIBILITY is gated by `controlled` ONLY (true = the CDP debugger is attached
 * and controlling; false/absent = the island is hidden) — it tracks the
 * attach/detach lifecycle, NOT WS connect. `kind` is the content; in THIS task
 * the SW only writes "ready" (controlled + idle) and "browser_tool" (with `tool`
 * = the cmd name) while a browser command runs. The thinking/tool/streaming/
 * confirm kinds are written by the chat UI in a later task.
 *
 * Wrapped so a missing session store (or eviction race) can never break command
 * execution.
 */
async function setIsland(
  controlled: boolean,
  kind?: string,
  tool?: string,
): Promise<void> {
  try {
    await chrome.storage.session.set({
      islandState: { controlled, kind: kind ?? "ready", tool },
    });
  } catch {
    // session storage unavailable — the island just won't update; non-fatal.
  }
}

// Account sync from the web app (auth ONLY — not a chat handoff). The main app
// pushes a one-time exchange code so the side-panel embed can create its own
// partitioned HttpOnly Session. Stored briefly under `embedExchangeCode` /
// `embedAgentSettings`; the side panel reads them via GET_BINDING / BINDING.
// Transport (webBase/wsBase) now comes from the baked config, not a handoff.
chrome.runtime.onMessageExternal.addListener(
  (msg: unknown, sender, sendResponse: (r: unknown) => void) => {
    const m = msg as Partial<AuthSyncMsg> | null;
    if (m?.type !== "AUTH_SYNC" && m?.type !== "AUTH_CLEAR") return false;
    if (!isAllowedWebAppSenderUrl(sender.url)) {
      sendResponse({ ok: false });
      return false;
    }

    void (async () => {
      if (m.type === "AUTH_CLEAR") {
        await chrome.storage.local.remove([
          "embedExchangeCode",
          "embedAgentSettings",
        ]);
        // Logout/account switch is a session-level close: detach every tab,
        // persist + publish the terminal event under the old session identity,
        // then close the old account's socket.
        await releaseControlledBrowserSession("auth_clear");
        await chrome.runtime.sendMessage({ type: "CLOSE_WS" }).catch(() => undefined);
        sendResponse({ ok: true });
        return;
      }
      if (typeof m.exchangeCode === "string" && m.exchangeCode)
        await chrome.storage.local.set({ embedExchangeCode: m.exchangeCode });
      if (m.agentSettings && typeof m.agentSettings === "object")
        await chrome.storage.local.set({ embedAgentSettings: m.agentSettings });
      sendResponse({ ok: true });
    })();

    return true; // keep the channel open for the async sendResponse
  },
);

async function requestAuthSyncFromOpenAppTabs(): Promise<void> {
  const tabs = await chrome.tabs.query({});
  const appTabs = tabs.filter((tab) => isAllowedWebAppSenderUrl(tab.url));
  if (appTabs.length === 0) return;

  // The content-script event causes the Web app to fetch a fresh exchange code
  // and send AUTH_SYNC back through onMessageExternal. Wait briefly for that
  // asynchronous round-trip; absence/failure falls back to independent login.
  await new Promise<void>((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      chrome.storage.onChanged.removeListener(onChanged);
      resolve();
    };
    const onChanged = (changes: Record<string, chrome.storage.StorageChange>, area: string) => {
      if (area === "local" && changes.embedExchangeCode?.newValue) finish();
    };
    chrome.storage.onChanged.addListener(onChanged);
    void Promise.allSettled(appTabs.map(async (tab) => {
      if (typeof tab.id !== "number") return;
      // Dispatch in the page's MAIN world. A declarative content script lives
      // in an isolated world: sending a message to it can succeed even when a
      // CustomEvent it dispatches does not reliably wake the application's
      // listener. That false success was why an authenticated main app could
      // still leave the side panel on its login screen. `host_permissions` is
      // generated from the exact externally_connectable allowlist, so this
      // never executes on arbitrary sites.
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        world: "MAIN",
        func: () => {
          document.dispatchEvent(
            new CustomEvent("flowork:extension-auth-refresh"),
          );
        },
      });
    }));
    setTimeout(finish, 1_500);
  });
}

// Connection lifecycle relayed up from the offscreen document.
chrome.runtime.onMessage.addListener((msg: unknown) => {
  const m = msg as { type?: string; echo?: unknown } | null;
  if (m?.type === "WS_OPEN") {
    const recovered = !state.connected;
    state.connected = true;
    if (recovered) {
      void liveRememberedControlledTabIds().then((ids) => {
        if (ids.length > 0) void setIsland(true, "recovered");
      });
    }
    void replayPendingBrowserTerminalEvent()
      .then(() => sendBrowserSessionSnapshot())
      .catch(() => undefined);
  }
  if (m?.type === "WS_CLOSED") {
    state.connected = false;
    void liveRememberedControlledTabIds().then((ids) => {
      if (ids.length > 0) void setIsland(true, "disconnected");
    });
  }
  if (m?.type === "WS_ECHO") {
    const echo = m.echo as Record<string, unknown> | undefined;
    if (echo?.type === "browser_session_event_ack") {
      void (async () => {
        const stored = await chrome.storage.session.get(
          "pendingBrowserSessionTerminalEvent",
        );
        const pending = stored.pendingBrowserSessionTerminalEvent as
          | Record<string, unknown>
          | undefined;
        if (
          pending &&
          String(pending.browser_session_id || "") === String(echo.browser_session_id || "") &&
          Number(pending.session_generation || 0) === Number(echo.session_generation || 0) &&
          Number(pending.event_seq || 0) === Number(echo.event_seq || 0)
        ) {
          await chrome.storage.session.remove("pendingBrowserSessionTerminalEvent");
        }
      })();
    }
    console.debug("[flowork] echo", m.echo);
  }
  return false;
});

// ---- Command execution, attach, and stop ----
chrome.runtime.onMessage.addListener(
  (msg: unknown, _sender, sendResponse: (r: unknown) => void) => {
    const m = msg as {
      type?: string;
      tabId?: number;
      windowId?: number;
      panelContextId?: string;
      chat_id?: string;
      turn_id?: string;
      browser_session_id?: string;
      session_generation?: number;
      event_seq?: number;
      env?: unknown;
    } | null;
    const type = m?.type;

    if (type === "AUTH_EXCHANGE_CONSUMED") {
      // Until this ACK arrives GET_BINDING/REQUEST_BINDING may replay the
      // one-time code so an iframe-load race cannot strand the side panel.
      void chrome.storage.local.remove("embedExchangeCode");
      sendResponse({ ok: true });
      return true;
    }

    if (type === "SIDEPANEL_WINDOW") {
      // The side panel tells us which window it lives in, so "adopt the current
      // tab" targets the active tab of THAT window (not whichever window happens
      // to be focused). Remember it in memory + storage (SW can restart).
      if (
        typeof m?.windowId === "number" &&
        typeof m.panelContextId === "string" &&
        m.panelContextId
      ) {
        activePanelWindowId = m.windowId;
        activePanelContextId = m.panelContextId;
        void chrome.storage.session.set({
          activePanelWindowId: m.windowId,
          activePanelContextId: m.panelContextId,
        });
      }
      return false;
    }

    if (type === "ISLAND_PHASE") {
      // The chat UI relays its current stream phase (ready/thinking/
      // tool/browser_tool/streaming) via the side-panel shell. Visibility stays
      // gated by debugger control, so only apply the phase while a tab is
      // actually controlled; otherwise the island must stay hidden. Coexists
      // with the Playwright relay lifecycle. Best-effort.
      try {
        const p = m as unknown as { kind?: string; tool?: string };
        if ((playwrightCdpBridge?.attachedTabIds().length ?? 0) > 0) {
          void setIsland(true, p.kind, p.tool);
        }
      } catch {
        // never let a relayed phase break the SW
      }
      return false; // no async response
    }

    if (type === "SET_LANG") {
      // Persist the language chosen in embedded settings so the
      // island content script (which reads chrome.storage.local.lang) renders in
      // the same language. Best-effort, validated to the closed zh/en set.
      try {
        const lang = (m as unknown as { lang?: string }).lang;
        if (lang === "zh" || lang === "en")
          void chrome.storage.local.set({ lang });
      } catch {
        // session/local storage unavailable — non-fatal
      }
      return false; // no async response
    }

    if (type === "SET_THEME") {
      try {
        const theme = (m as unknown as { theme?: string }).theme;
        if (theme === "light" || theme === "dark")
          void chrome.storage.local.set({ theme });
      } catch {
        // Theme projection is presentation-only and must never disrupt control.
      }
      return false;
    }

    if (type === "BROWSER_TURN_CANCELLED") {
      // Turn cancellation is enforced by the backend Runtime. The extension
      // owns no command queue in the Playwright design, so acknowledgement is
      // sufficient; closing the CDP transport remains a separate lifecycle act.
      sendResponse({ ok: true });
      return true;
    }

    if (type === "PLAYWRIGHT_RELAY_FRAME") {
      void (async () => {
        await browserSessionStateReady;
        const env = m!.env as {
          id?: unknown;
          channel?: unknown;
          transport?: unknown;
          data?: {
            action?: unknown;
            browser_session_id?: unknown;
            session_generation?: unknown;
            request?: unknown;
          };
        };
        const id = String(env?.id || "");
        const channel = String(env?.channel || "");
        const transport = String(env?.transport || "");
        const data = env?.data ?? {};
        const response = (message: CDPMessage | Record<string, unknown>) =>
          encode("playwright_relay", {
            id,
            channel,
            transport,
            data: { action: "message", message },
          });
        const fail = (message: string) =>
          response({ error: { code: -32603, message } });

        const action = String(data.action || "");
        if (action === "initialize") {
          const incomingSessionId = String(data.browser_session_id || "");
          const incomingGeneration = Number(data.session_generation || 0);
          if (!incomingSessionId || !Number.isInteger(incomingGeneration) || incomingGeneration <= 0) {
            sendResponse(fail("Playwright initialization is missing its browser-session fence"));
            return;
          }
          const windowId = Number(activePanelWindowId);
          if (!Number.isInteger(windowId) || windowId < 0) {
            sendResponse(fail("The side-panel browser window is unavailable"));
            return;
          }

          const previous = currentBrowserSession;
          const sameSession =
            previous?.sessionId === incomingSessionId &&
            Number(previous?.sessionGeneration || 0) === incomingGeneration &&
            previous?.channel === channel;
          const rememberedTabs = sameSession
            ? await rememberedControlledTabIds()
            : [];
          const tabs: RelayTab[] = [];
          for (const tabId of rememberedTabs) {
            try {
              const tab = (await chrome.tabs.get(tabId)) as RelayTab;
              if (tab.windowId === windowId) tabs.push(tab);
            } catch {
              // Closed tabs are omitted from the fresh Playwright handshake.
            }
          }
          if (tabs.length === 0) {
            const [active] = await chrome.tabs.query({ active: true, windowId });
            if (
              active?.id === undefined ||
              !active.url ||
              !/^(https?|file):/.test(active.url)
            ) {
              sendResponse(fail("No controllable active page is available in the side-panel window"));
              return;
            }
            tabs.push(active as RelayTab);
          }

          await closePlaywrightCdpBridge();
          // An extension update may leave Chrome's debugger attached while the
          // previous service worker is being replaced. Detach the extension's
          // own stale attachment before official Playwright takes ownership.
          for (const tab of tabs) {
            if (tab.id === undefined) continue;
            try {
              await chrome.debugger.detach({ tabId: tab.id });
            } catch {
              // A service-worker restart may already have released it.
            }
          }
          await chrome.storage.session.set({
            controlledTabId: tabs[0]?.id,
            controlledTabIds: tabs
              .map((tab) => tab.id)
              .filter((tabId): tabId is number => typeof tabId === "number"),
          });
          await setCurrentBrowserSession({
            sessionId: incomingSessionId,
            channel,
            transport,
            chatId: chatIdFromChannel(channel),
            browserWindowId: String(windowId),
            panelContextId: activePanelContextId,
            sessionGeneration: incomingGeneration,
          });

          const emit = (message: CDPMessage) => {
            void chrome.runtime.sendMessage({
              type: "WS_SEND",
              raw: encode("playwright_relay", {
                id: `pw_evt_${Date.now()}_${++browserSessionEventSeq}`,
                channel,
                transport,
                data: {
                  action: "message",
                  browser_session_id: incomingSessionId,
                  session_generation: incomingGeneration,
                  message,
                },
              }),
            });
          };
          playwrightCdpBridge = new PlaywrightCdpBridge(
            playwrightRelayChrome,
            windowId,
            emit,
            console.error,
            handlePlaywrightDebuggerDetach,
            (tabIds, reason, tabId) => {
              void persistPlaywrightControlledTabs(tabIds);
              if (reason === "attached") {
                void setIsland(true, "ready");
              } else if (reason === "tab_removed" && tabIds.length === 0) {
                void releaseControlledBrowserSession("last_tab_closed", { tabId });
              }
            },
          );
          playwrightCdpBridge.initialize(tabs);
          // The backend confirms the durable lease over the authenticated CDP
          // socket, while the embedded UI needs the same ownership transition
          // immediately. Without this projection the Chat list eventually
          // shows `attached` but the side panel still believes no local window
          // owns it and disables every later message as "another window".
          await broadcastBrowserSessionChanged({
            status: "attached",
            reason: "playwright_initialized",
            tab_ids: tabs
              .map((tab) => tab.id)
              .filter((tabId): tabId is number => typeof tabId === "number"),
          });
          sendResponse(response({ result: { initialized: true, tabs: tabs.length } }));
          return;
        }

        const session = currentBrowserSession;
        if (!session?.sessionId || !session.channel) {
          sendResponse(fail("No active Flowork browser session"));
          return;
        }
        if (
          channel !== session.channel ||
          String(data.browser_session_id || "") !== session.sessionId ||
          Number(data.session_generation || 0) !== Number(session.sessionGeneration || 0)
        ) {
          sendResponse(fail("Playwright relay frame belongs to another browser session"));
          return;
        }
        if (action === "close") {
          await closePlaywrightCdpBridge();
          sendResponse(response({ result: { closed: true } }));
          return;
        }
        if (action !== "request" || !playwrightCdpBridge) {
          sendResponse(fail("Playwright CDP bridge is not initialized"));
          return;
        }
        const request = data.request as CDPMessage | undefined;
        if (!request || typeof request !== "object") {
          sendResponse(fail("Playwright relay request is missing"));
          return;
        }
        sendResponse(response(await playwrightCdpBridge.handle(request)));
      })().catch((error) => {
        sendResponse(
          encode("playwright_relay", {
            id: String((m!.env as { id?: unknown })?.id || ""),
            channel: String((m!.env as { channel?: unknown })?.channel || ""),
            transport: String((m!.env as { transport?: unknown })?.transport || ""),
            data: {
              action: "message",
              message: {
                error: { code: -32603, message: String((error as Error)?.message || error) },
              },
            },
          }),
        );
      });
      return true;
    }

    if (type === "GET_BINDING") {
      // The side-panel iframe asks for the current binding so it can build its
      // /embed/chat URL. We also hand back a one-time Session exchange code.
      void (async () => {
        await browserSessionStateReady;
        const browserId = await getStableBrowserId();
        const { embedExchangeCode, embedAgentSettings } =
          await chrome.storage.local.get([
            "embedExchangeCode",
            "embedAgentSettings",
          ]);
        sendResponse({
          wf_id: binding.wf_id,
          browser_id: browserId,
          // A cold side panel starts without a relayed chat id. Once the
          // The authenticated Playwright initialize frame on `chat:<id>` gives
          // the extension an authoritative local projection of the controlled
          // Chat. Reuse it on shell/iframe reload so the embed resumes the same
          // server-owned history instead of minting a new conversation.
          chat_id: currentBrowserSession?.chatId || binding.chat_id,
          browser_control_chat_id: currentBrowserSession?.chatId || "",
          browser_control_available_here:
            !currentBrowserSession?.chatId ||
            (typeof m?.windowId === "number" &&
              String(m.windowId) === String(currentBrowserSession.browserWindowId || "")),
          // App Relay: the relayed instruction for the embed to
          // auto-send once. Empty when not relayed (entry B → no auto-send).
          instruction: binding.instruction,
          webBase: binding.webBase,
          exchangeCode:
            typeof embedExchangeCode === "string" ? embedExchangeCode : "",
          agentSettings:
            embedAgentSettings && typeof embedAgentSettings === "object"
              ? embedAgentSettings
              : undefined,
        });
      })();
      return true; // async sendResponse
    }

    if (type === "REQUEST_BINDING") {
      // Entry B: the iframe (relayed by the side-panel shell) requests a fresh
      // binding broadcast. Same payload as GET_BINDING.
      void (async () => {
        await browserSessionStateReady;
        const browserId = await getStableBrowserId();
        const { embedExchangeCode, embedAgentSettings } =
          await chrome.storage.local.get([
            "embedExchangeCode",
            "embedAgentSettings",
          ]);
        sendResponse({
          type: "BINDING",
          wf_id: binding.wf_id,
          browser_id: browserId,
          chat_id: currentBrowserSession?.chatId || binding.chat_id,
          browser_control_chat_id: currentBrowserSession?.chatId || "",
          browser_control_available_here:
            !currentBrowserSession?.chatId ||
            (typeof m?.windowId === "number" &&
              String(m.windowId) === String(currentBrowserSession.browserWindowId || "")),
          // App Relay: the relayed instruction for the embed to
          // auto-send once. Empty when not relayed (entry B → no auto-send).
          instruction: binding.instruction,
          webBase: binding.webBase,
          exchangeCode:
            typeof embedExchangeCode === "string" ? embedExchangeCode : "",
          agentSettings:
            embedAgentSettings && typeof embedAgentSettings === "object"
              ? embedAgentSettings
              : undefined,
        });
      })();
      return true; // async sendResponse
    }

    if (type === "REQUEST_AUTH_REFRESH") {
      void (async () => {
        await requestAuthSyncFromOpenAppTabs();
        const browserId = await getStableBrowserId();
        const { embedExchangeCode, embedAgentSettings } =
          await chrome.storage.local.get(["embedExchangeCode", "embedAgentSettings"]);
        sendResponse({
          type: "BINDING",
          wf_id: binding.wf_id,
          browser_id: browserId,
          chat_id: currentBrowserSession?.chatId || binding.chat_id,
          browser_control_chat_id: currentBrowserSession?.chatId || "",
          browser_control_available_here:
            !currentBrowserSession?.chatId ||
            (typeof m?.windowId === "number" &&
              String(m.windowId) === String(currentBrowserSession.browserWindowId || "")),
          instruction: binding.instruction,
          webBase: binding.webBase,
          exchangeCode:
            typeof embedExchangeCode === "string" ? embedExchangeCode : "",
          agentSettings:
            embedAgentSettings && typeof embedAgentSettings === "object"
              ? embedAgentSettings
              : undefined,
        });
      })();
      return true;
    }

    // Entry B: the iframe relays `{type:"OPEN_WS", scopedToken}` (via the side
    // panel shell). We open the WS through the offscreen. NOTE: the SW→offscreen
    // relay below uses `token` (not `scopedToken`), so this branch — keyed on
    // `scopedToken` — never re-fires on its own relay (no loop).
    const o = m as unknown as {
      type?: string;
      wsBase?: string;
      scopedToken?: string;
      browser?: string;
    };
    if (type === "OPEN_WS" && typeof o.scopedToken === "string") {
      void (async () => {
        await ensureOffscreen();
        const browser = o.browser || (await getStableBrowserId());
        binding.browser_id = browser;
        const opened = await chrome.runtime.sendMessage({
          // Internal-only name prevents the offscreen document from racing the
          // public iframe OPEN_WS broadcast before this worker has validated
          // and completed its transport configuration.
          type: "OPEN_WS_INTERNAL",
          // The embed's relay often omits wsBase → fall back to the handoff's
          // stored value, then to the baked WS_BASE (a COLD entry-B open has
          // neither) so the offscreen never builds a relative (chrome-
          // extension://) URL.
          wsBase: o.wsBase || binding.wsBase || WS_BASE,
          token: o.scopedToken,
          browser,
        });
        sendResponse({
          ok:
            typeof opened === "object" &&
            opened !== null &&
            (opened as { ok?: unknown }).ok === true,
        });
      })();
      return true; // async sendResponse
    }

    if (type === "STOP") {
      // Island Stop cancels the current Agent Turn via the side-panel iframe.
      // It must not release browser control or close the browser WebSocket.
      void chrome.runtime.sendMessage({ type: "BROWSER_STOP_REQUESTED" }).catch(() => {
        // No side panel currently open; there is no active composer to cancel.
      });
      sendResponse({ ok: true });
      return true;
    }

    return false;
  },
);
