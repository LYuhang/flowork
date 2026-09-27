/**
 * Offscreen document entry (B1). The offscreen page is the ONE place that holds
 * the backend WebSocket, because a persistent socket must outlive the MV3
 * service worker (which Chrome evicts when idle).
 *
 * It does NOT run CDP: `chrome.debugger` is unavailable in offscreen documents.
 * Official Playwright relay frames are forwarded to the service worker, while
 * lifecycle events travel in the other direction through `WS_SEND`.
 */
import { WsClient } from "./shared/ws-client";
import { browserWsProtocols } from "./shared/browser-ws-auth";

interface OpenWsMsg {
  type: "OPEN_WS_INTERNAL";
  wsBase: string;
  token: string;
  browser: string;
}
interface PingMsg {
  type: "WS_PING";
  data: unknown;
}
interface WsSendMsg {
  type: "WS_SEND";
  raw: string;
}
interface CloseWsMsg {
  type: "CLOSE_WS";
}
interface ClipboardWriteMsg {
  type: "CLIPBOARD_WRITE";
  text: string;
  html: string;
}
type OffscreenMsg =
  | OpenWsMsg
  | PingMsg
  | WsSendMsg
  | CloseWsMsg
  | ClipboardWriteMsg
  | { type?: undefined };

let client: WsClient | null = null;
let clientKey = "";
let clientEndpoint = "";

// Keep the service worker alive while this (persistent) offscreen document
// exists, so the SW-held chrome.debugger session isn't released by SW eviction
// (which would drop the controlled-tab attach + the "debugging" banner). A
// connected port keeps the SW alive; we reconnect well under Chrome's 5-min cap.
function keepSwAlive(): void {
  const port = chrome.runtime.connect({ name: "keepalive" });
  // An open port alone is not reliably enough — periodic ACTIVITY on it resets
  // the SW's ~30s idle timer. Ping every 20s so the SW (and its chrome.debugger
  // session) never gets evicted while a session is active.
  const ping = setInterval(() => {
    try {
      port.postMessage("ping");
    } catch {
      /* port dead — onDisconnect will reconnect */
    }
  }, 20_000);
  port.onDisconnect.addListener(() => {
    clearInterval(ping);
    setTimeout(keepSwAlive, 1000);
  });
  setTimeout(() => {
    try {
      port.disconnect();
    } catch {
      /* already gone */
    }
  }, 240_000);
}
keepSwAlive();

chrome.runtime.onMessage.addListener(
  (msg: unknown, _sender, sendResponse: (r: unknown) => void) => {
    const m = msg as OffscreenMsg | null;

    if (m?.type === "OPEN_WS_INTERNAL") {
      // Guard a missing/relative wsBase: without an absolute ws(s):// base the
      // URL would resolve against chrome-extension:// and `new WebSocket` throws
      // "scheme … not allowed". Bail loudly instead of crashing the handler.
      if (!/^wss?:\/\//i.test(m.wsBase ?? "")) {
        console.error("[offscreen] OPEN_WS got an invalid wsBase:", m.wsBase);
        sendResponse({ ok: false, error: "invalid wsBase" });
        return true;
      }
      const url = `${m.wsBase.replace(/\/+$/, "")}/api/v1/browser/ws`;
      let protocols: string[];
      try {
        protocols = browserWsProtocols(m.token, m.browser);
      } catch {
        sendResponse({ ok: false, error: "invalid browser WebSocket authentication" });
        return true;
      }
      const nextKey = JSON.stringify([url, protocols]);
      if (client && clientKey === nextKey && client.isActive()) {
        sendResponse({ ok: true, reused: true });
        return false;
      }
      void (async () => {
      const previous = client;
      if (previous && clientEndpoint === url && previous.isActive()
          && await previous.refreshAuthentication(m.token, protocols)) {
        if (client !== previous) { sendResponse({ ok: false }); return; }
        clientKey = nextKey;
        sendResponse({ ok: true, reused: true }); return;
      }
      if (client !== previous) { sendResponse({ ok: false }); return; }
      // Exactly one socket per attached browser.
      client?.disconnect();
      const connected = new WsClient(url, protocols);
      client = connected;
      clientKey = nextKey;
      clientEndpoint = url;
      connected.onOpen(() => { if (client === connected) void chrome.runtime.sendMessage({ type: "WS_OPEN" }); });
      connected.onClose(() => { if (client === connected) void chrome.runtime.sendMessage({ type: "WS_CLOSED" }); });
      connected.onAuthRequired(() => { if (client === connected) void chrome.runtime.sendMessage({ type: "WS_AUTH_REQUIRED" }); });
      connected.onEcho((echo) => { if (client === connected) void chrome.runtime.sendMessage({ type: "WS_ECHO", echo }); });
      connected.onPlaywrightRelay((env) => {
        if (client !== connected) return;
        chrome.runtime.sendMessage(
          { type: "PLAYWRIGHT_RELAY_FRAME", env },
          (relayRaw) => {
            if (chrome.runtime.lastError || client !== connected) return;
            if (typeof relayRaw === "string" && !connected.sendRaw(relayRaw)) {
              console.warn("[offscreen] dropped Playwright relay response without an active socket");
            }
          },
        );
      });
      connected.connect();
      sendResponse({ ok: true });
      })().catch(() => sendResponse({ ok: false }));
      return true;
    }

    if (m?.type === "WS_PING") {
      const id = client?.ping(m.data);
      sendResponse({ ok: Boolean(id), id });
      return true;
    }

    if (m?.type === "WS_SEND") {
      // The SW pushes a pre-encoded frame (e.g. a tab event) to the backend.
      const accepted = client?.sendRaw(m.raw) === true;
      sendResponse({ ok: accepted });
      return false;
    }

    if (m?.type === "CLOSE_WS") {
      client?.disconnect();
      client = null;
      clientKey = "";
      clientEndpoint = "";
      sendResponse({ ok: true });
      return false;
    }

    if (m?.type === "CLIPBOARD_WRITE") {
      // Write-only bridge for one native rich-text paste transaction. The
      // extension never reads or returns the user's previous clipboard.
      void (async () => {
        try {
          // Offscreen documents cannot become the focused document required by
          // navigator.clipboard.write(). The extension clipboard permission
          // does, however, allow the native copy command. Supplying both MIME
          // flavors in the copy event avoids depending on a DOM selection or
          // focus state (both are unreliable in a hidden offscreen document).
          const onCopy = (event: ClipboardEvent) => {
            event.preventDefault();
            event.clipboardData?.setData("text/plain", m.text);
            event.clipboardData?.setData("text/html", m.html);
          };
          document.addEventListener("copy", onCopy, { once: true });
          const copied = document.execCommand("copy");
          document.removeEventListener("copy", onCopy);
          if (!copied) throw new Error("native clipboard copy command was rejected");
          sendResponse({ ok: true });
        } catch (error) {
          sendResponse({
            ok: false,
            error: String((error as Error)?.message || error),
          });
        }
      })();
      return true;
    }

    return false;
  },
);
