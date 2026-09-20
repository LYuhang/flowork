/**
 * Side-panel shell. The panel is a thin host that mounts
 * an <iframe> pointing at the web app's `/embed/chat` route (the real chat UI is
 * the existing AgentChatSidebar, reused — never rebuilt) and bridges messages
 * between that iframe and the service worker:
 *
 *   iframe → shell:  postMessage {type:"REQUEST_BINDING"}  → SW REQUEST_BINDING
 *                    postMessage {type:"OPEN_WS", scopedToken} → SW OPEN_WS
 *   shell  → iframe: SW responses relayed back via contentWindow.postMessage
 *                    (e.g. {type:"BINDING", wf_id, chat_id, exchangeCode})
 *
 * The embed header carries the workflow info, so the shell keeps no visible
 * chrome of its own beyond a tiny "connecting…" fallback.
 */
import { resolveAllowedWebBase, WEB_BASE } from "./shared/config";
import { projectBrowserControlForWindow } from "./shared/browser-control-projection";

interface Binding {
  wf_id: string;
  browser_id: string;
  chat_id: string;
  browser_control_chat_id?: string;
  browser_control_available_here?: boolean;
  /** App Relay — the in-flight instruction relayed from the main
   *  app for the embed to auto-send once. Empty when not relayed (entry B). */
  instruction?: string;
  exchangeCode: string;
  /** Main-app model settings forwarded into the iframe so
   *  the embedded chat uses the same credential and generation parameters. */
  agentSettings?: Record<string, unknown>;
  /** The web app's full RUNTIME base (origin + proxy path prefix, e.g.
   *  https://host/pws…). Used to build the /embed/chat URL so it carries the
   *  prefix. Empty → fall back to the bundled WEB_BASE (root deploys). */
  webBase?: string;
}

/** The origin the embed iframe is loaded from (web app's origin). Defaults to
 *  WEB_BASE's origin; updated from the binding's `webBase` once known. Used for
 *  the postMessage targetOrigin + the inbound message-origin guard. */
let embedOrigin = ((): string => {
  try {
    return new URL(WEB_BASE).origin;
  } catch {
    return WEB_BASE;
  }
})();

const iframe = document.getElementById("embed") as HTMLIFrameElement | null;
const statusEl = document.getElementById("shell-status");
const statusTitleEl = document.getElementById("status-title");
const statusDetailEl = document.getElementById("status-detail");
const retryEl = document.getElementById("retry") as HTMLButtonElement | null;
const panelContextId =
  (typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `panel_${Date.now()}_${Math.random().toString(16).slice(2)}`);
let currentWindowId: number | undefined;
let shellLang: "zh" | "en" = "en";
let shellTheme: "light" | "dark" | undefined;
let currentBinding: Binding | null = null;
let loadTimer: ReturnType<typeof setTimeout> | undefined;

const SHELL_COPY = {
  zh: {
    loading: ["正在连接…", "正在打开浏览器对话"],
    auth: ["需要登录", "请在对话面板中登录后继续"],
    unavailable: ["暂时无法打开对话", "请检查应用服务后重试"],
    retry: "重试",
    frameTitle: "Flowork 对话",
  },
  en: {
    loading: ["Connecting…", "Opening browser chat"],
    auth: ["Sign in required", "Sign in in the chat panel to continue"],
    unavailable: ["Chat is unavailable", "Check the app service and try again"],
    retry: "Retry",
    frameTitle: "Flowork chat",
  },
} as const;

function showShellState(state: "loading" | "auth" | "unavailable"): void {
  const copy = SHELL_COPY[shellLang];
  const [title, detail] = copy[state];
  if (statusTitleEl) statusTitleEl.textContent = title;
  if (statusDetailEl) statusDetailEl.textContent = detail;
  if (retryEl) {
    retryEl.textContent = copy.retry;
    retryEl.hidden = state !== "unavailable";
  }
  if (statusEl) statusEl.hidden = false;
}

function hideShellState(): void {
  if (statusEl) statusEl.hidden = true;
}

function applyShellTheme(theme: unknown): void {
  shellTheme = theme === "dark" ? "dark" : theme === "light" ? "light" : undefined;
  if (shellTheme) document.documentElement.dataset.theme = shellTheme;
  else delete document.documentElement.dataset.theme;
}

function beginIframeLoad(binding: Binding): void {
  if (!iframe) return;
  const allowedBase = resolveAllowedWebBase(binding.webBase);
  if (!allowedBase) {
    currentBinding = null;
    showShellState("unavailable");
    return;
  }
  currentBinding = binding;
  showShellState(binding.exchangeCode ? "loading" : "auth");
  iframe.title = SHELL_COPY[shellLang].frameTitle;
  iframe.src = buildEmbedUrl(binding, allowedBase);
  if (loadTimer) clearTimeout(loadTimer);
  loadTimer = setTimeout(() => showShellState("unavailable"), 12_000);
}

function sendToSw<T = unknown>(msg: unknown): Promise<T | undefined> {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(msg, (r: T) => {
        // Swallow "no receiving end" etc. — the bridge stays best-effort.
        void chrome.runtime.lastError;
        resolve(r);
      });
    } catch {
      resolve(undefined);
    }
  });
}

function buildEmbedUrl(b: Binding, allowedBase = resolveAllowedWebBase(b.webBase)): string {
  // Use the web app's RUNTIME base (origin + proxy path prefix). Build by
  // string-join, NOT `new URL("/embed/chat", base)` — a leading-slash path
  // resolves against the ORIGIN and would DROP the prefix (e.g. /pws…).
  if (!allowedBase) throw new Error("extension binding web base is not allowlisted");
  const base = allowedBase.replace(/\/+$/, "");
  const u = new URL(`${base}/embed/chat`);
  u.searchParams.set("mode", "browser");
  if (b.wf_id) u.searchParams.set("wf", b.wf_id);
  // App Relay: thread the RELAYED chat_id so the embed loads the
  // SAME conversation. Entry B has no relayed chat → the embed mints a fresh
  // uuid (unchanged).
  if (b.chat_id) u.searchParams.set("chat", b.chat_id);
  // App Relay: thread the relayed instruction so the embed auto-sends it once.
  if (b.instruction) u.searchParams.set("instruction", b.instruction);
  return u.toString();
}

function postToIframe(msg: unknown): void {
  // Target origin = the embed's origin so we never leak to a navigated-away frame.
  iframe?.contentWindow?.postMessage(msg, embedOrigin);
}

// Tell the SW which window this side panel is docked in, so "adopt the current
// tab" targets THIS window's active tab (not whichever window is focused). Report
// on mount and whenever the panel regains focus (the user may have switched
// windows, each with its own side panel).
async function reportWindow(): Promise<void> {
  try {
    const w = await chrome.windows.getCurrent();
    if (typeof w?.id === "number") {
      currentWindowId = w.id;
      void sendToSw({ type: "SIDEPANEL_WINDOW", windowId: w.id, panelContextId });
    }
  } catch {
    /* windows API unavailable — non-fatal */
  }
}

async function mount(): Promise<void> {
  try {
    const stored = await chrome.storage.local.get(["lang", "theme"]);
    shellLang = stored.lang === "zh" ? "zh" : "en";
    applyShellTheme(stored.theme);
    document.documentElement.lang = shellLang === "zh" ? "zh-CN" : "en";
  } catch {
    // Keep the English-first open-source default.
  }
  showShellState("loading");
  await reportWindow();
  window.addEventListener("focus", () => void reportWindow());
  const b = (await sendToSw<Binding>({
    type: "GET_BINDING",
    panelContextId,
    windowId: currentWindowId,
  })) ?? {
    wf_id: "",
    browser_id: "",
    chat_id: "",
    browser_control_chat_id: "",
    browser_control_available_here: true,
    exchangeCode: "",
  };
  // Lock the embed origin to the web app's runtime base (binding webBase, else
  // the bundled WEB_BASE) for the postMessage targetOrigin + inbound guard.
  const allowedBase = resolveAllowedWebBase(b.webBase);
  if (allowedBase) embedOrigin = new URL(allowedBase).origin;
  if (iframe) {
    // Entry A: the iframe gets wf/chat/browser from the URL but never issues a
    // REQUEST_BINDING, so it would otherwise never receive the relayed agent
    // settings. Push a BINDING into the iframe once it loads so the embed can
    // seed the credential/model settings. (Entry B also
    // gets a BINDING via its own REQUEST_BINDING; a second one is idempotent.)
    iframe.addEventListener("load", () => {
      if (loadTimer) clearTimeout(loadTimer);
      hideShellState();
      postToIframe({ type: "BINDING", ...b });
    });
    iframe.addEventListener("error", () => showShellState("unavailable"));
    beginIframeLoad(b);
  }
}

retryEl?.addEventListener("click", () => {
  if (currentBinding) beginIframeLoad(currentBinding);
  else void mount();
});

void mount();

// Bridge: messages from the embedded chat iframe → service worker, responses
// relayed back into the iframe. We only act on our own embed origin.
window.addEventListener("message", (ev: MessageEvent) => {
  if (ev.source !== iframe?.contentWindow) return;
  if (embedOrigin && ev.origin !== embedOrigin) return;
  const m = ev.data as {
    type?: string;
    scopedToken?: string;
    kind?: string;
    tool?: string;
    lang?: string;
    theme?: string;
    chat_id?: string;
    turn_id?: string;
  } | null;
  if (!m?.type) return;

  if (m.type === "ISLAND_PHASE") {
    // Forward the agent's relayed chat-stream phase
    // it to the SW (which gates visibility on debugger control). Fire-and-forget.
    void sendToSw({ type: "ISLAND_PHASE", kind: m.kind, tool: m.tool });
  } else if (m.type === "SET_LANG") {
    // Forward the language selected in embedded settings to the service worker,
    // which persists it to chrome.storage.local for the island. Fire-and-forget.
    void sendToSw({ type: "SET_LANG", lang: m.lang });
    shellLang = m.lang === "zh" ? "zh" : "en";
    document.documentElement.lang = shellLang === "zh" ? "zh-CN" : "en";
    if (iframe) iframe.title = SHELL_COPY[shellLang].frameTitle;
  } else if (m.type === "SET_THEME") {
    applyShellTheme(m.theme);
    void sendToSw({ type: "SET_THEME", theme: shellTheme });
  } else if (m.type === "BROWSER_TURN_CANCELLED") {
    void sendToSw({
      type: "BROWSER_TURN_CANCELLED",
      chat_id: m.chat_id,
      turn_id: m.turn_id,
    });
  } else if (m.type === "AUTH_EXCHANGE_CONSUMED") {
    // Retain the single-use code until the iframe confirms that its
    // partitioned HttpOnly Session exists. This closes the iframe-load race.
    void sendToSw({ type: "AUTH_EXCHANGE_CONSUMED" });
  } else if (m.type === "REQUEST_BINDING") {
    void sendToSw<Binding & { type?: string }>({
      type: "REQUEST_BINDING",
      panelContextId,
      windowId: currentWindowId,
    }).then(
      (r) => {
        if (r) postToIframe({ type: "BINDING", ...r });
      },
    );
  } else if (m.type === "REQUEST_AUTH_REFRESH") {
    void sendToSw<Binding & { type?: string }>({
      type: "REQUEST_AUTH_REFRESH",
      panelContextId,
      windowId: currentWindowId,
    }).then((r) => {
      if (r) postToIframe({ type: "BINDING", ...r });
    });
  } else if (m.type === "OPEN_WS") {
    void sendToSw<{ ok?: boolean }>({
      type: "OPEN_WS",
      scopedToken: m.scopedToken,
    }).then((r) => postToIframe({ type: "OPEN_WS_RESULT", ok: !!r?.ok }));
  }
});

// Reflect WS connection state into the tiny fallback chip (the iframe is the
// primary UI; this only matters before/around the iframe mounting).
chrome.runtime.onMessage.addListener((msg: unknown) => {
  const m = msg as {
    type?: string;
    chat_id?: string;
    status?: string;
    browser_window_id?: string | number;
  } | null;
  if (m?.type === "WS_OPEN" && statusEl) statusEl.hidden = true;
  if (m?.type === "WS_AUTH_REQUIRED") {
    postToIframe({ type: "BROWSER_WS_AUTH_REQUIRED" });
  }
  if (m?.type === "BROWSER_SESSION_CHANGED") {
    postToIframe(projectBrowserControlForWindow(
      m as unknown as Record<string, unknown>,
      currentWindowId,
    ));
  }
  if (m?.type === "BROWSER_STOP_REQUESTED") postToIframe(m);
  return false;
});
