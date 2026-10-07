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
import type { CookieConsent } from "./playwright/cookie-consent";

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

let quoteContext: { chatId: string; account: string } | null = null;

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
let cookieConsents: CookieConsent[] = [];
let cookieDecisionPending = false;
const cookiePanel = document.getElementById("cookie-permissions") as HTMLDetailsElement | null;
const cookieSites = document.getElementById("cookie-sites");
const cookieFeedback = document.getElementById("cookie-feedback");
type LocalDownloadConfirmation = { capture_id: string; files: { id: string; name: string; bytes: number }[] };
let localDownloadConfirmation: LocalDownloadConfirmation | null = null;
let localDownloadSelection: string | null = null;
let localDownloadBusy = false;
let localDownloadFeedback = "";
let localDownloadRevision = 0;
function renderLocalDownload(): void {
  const panel = document.getElementById("download-confirmation");
  if (!panel) return;
  panel.hidden = !localDownloadConfirmation;
  panel.replaceChildren();
  if (!localDownloadConfirmation) return;
  const zh = shellLang === "zh";
  const title = document.createElement("h2"); title.id = "download-confirm-title";
  title.textContent = zh ? "确认本地下载文件" : "Confirm local download";
  const explanation = document.createElement("p");
  explanation.textContent = zh
    ? "浏览器无法确定文件来自哪个标签页。请仅选择本次请求的文件；也可能包含其他窗口的下载。确认前，文件信息仅在此插件中显示。传输仍遵循当前审批模式。"
    : "Chrome cannot identify the source tab. Select only the file requested now; downloads from other windows may appear. File details stay in this extension until confirmed. Transfer still follows your approval mode.";
  panel.append(title, explanation);
  for (const file of localDownloadConfirmation.files) {
    const label = document.createElement("label"), radio = document.createElement("input"), text = document.createElement("span");
    radio.type = "radio"; radio.name = "local-download"; radio.value = file.id;
    radio.checked = localDownloadSelection === file.id; radio.disabled = localDownloadBusy;
    radio.addEventListener("change", event => {
      if (!event.isTrusted) return;
      localDownloadSelection = file.id;
      const confirm = panel.querySelector<HTMLButtonElement>("[data-download-confirm]");
      if (confirm) confirm.disabled = localDownloadBusy;
    });
    text.textContent = `${file.name} · ${file.bytes.toLocaleString()} bytes`;
    label.append(radio, text); panel.append(label);
  }
  const actions = document.createElement("div"); actions.className = "cookie-actions";
  for (const allow of [false, true]) {
    const button = document.createElement("button"); button.type = "button"; button.className = "cookie-action";
    button.textContent = allow ? (zh ? "确认文件" : "Confirm file") : (zh ? "取消" : "Cancel");
    button.disabled = localDownloadBusy || (allow && !localDownloadSelection);
    if (allow) { button.dataset.primary = "true"; button.dataset.downloadConfirm = "true"; }
    button.addEventListener("click", event => { if (event.isTrusted) void decideLocalDownload(allow); });
    actions.append(button);
  }
  const feedback = document.createElement("p"); feedback.setAttribute("role", "status"); feedback.textContent = localDownloadFeedback;
  panel.append(actions, feedback);
}
async function refreshLocalDownload(): Promise<void> {
  const revision = ++localDownloadRevision;
  const response = await sendToSw<{ ok?: boolean; confirmation?: LocalDownloadConfirmation | null }>({
    type: "DOWNLOAD_CONFIRM_LIST", panelContextId, windowId: currentWindowId,
  });
  if (revision !== localDownloadRevision || !response?.ok || localDownloadBusy) return;
  const next = response.confirmation ?? null;
  if (JSON.stringify(next) === JSON.stringify(localDownloadConfirmation)) return;
  if (next?.capture_id !== localDownloadConfirmation?.capture_id) localDownloadFeedback = "";
  localDownloadConfirmation = next;
  if (!next?.files.some(file => file.id === localDownloadSelection))
    localDownloadSelection = next?.files.length === 1 ? next.files[0].id : null;
  renderLocalDownload();
}
async function decideLocalDownload(allow: boolean): Promise<void> {
  if (localDownloadBusy || !localDownloadConfirmation || (allow && !localDownloadSelection)) return;
  localDownloadBusy = true; ++localDownloadRevision;
  const captureId = localDownloadConfirmation.capture_id;
  renderLocalDownload();
  const response = await sendToSw<{ ok?: boolean; confirmation?: LocalDownloadConfirmation | null }>({
    type: "DOWNLOAD_CONFIRM_DECIDE", capture_id: captureId, file_id: allow ? localDownloadSelection : null,
    panelContextId, windowId: currentWindowId,
  });
  localDownloadBusy = false;
  if (response?.ok) localDownloadConfirmation = response.confirmation ?? null;
  else localDownloadFeedback = shellLang === "zh" ? "确认已失效或文件发生变化，请检查下载记录。" : "Confirmation expired or the file changed. Inspect browser Downloads.";
  renderLocalDownload(); void refreshLocalDownload();
}
// Also restores pending confirmation after reloading the shell and removes
// expired controls even if a one-shot service-worker notification was missed.
setInterval(() => { void refreshLocalDownload(); }, 1000);
const COOKIE_COPY = {
  zh: {
    title: "Cookie 导出权限",
    explanation: "允许 Agent 在本次浏览器控制期间，将所列站点的 Cookie（可能包含登录凭证）导出到云端沙盒获取资源。文件不会进入普通预览、分享或持久化存储，并在撤销授权或本轮执行结束后清理。普通网页操作无需此权限。",
    requested: "等待您决定；Agent 无法自行授权。",
    allowed: "已允许。可以告诉 Agent 继续；您可随时撤销。",
    denied: "已拒绝。正常浏览和页面交互不受影响。",
    allow: "允许导出", deny: "拒绝", revoke: "撤销授权", updating: "正在更新权限…",
    failed: "权限更新失败，请重试。", updated: "权限已更新。",
  },
  en: {
    title: "Cookie export permissions",
    explanation: "Allow the Agent to export Cookies, which may include login credentials, for the listed sites into your cloud sandbox during this browser-control session. Exported files are excluded from normal previews, sharing and durable storage, and are removed on revocation or when this Agent turn ends. Normal page interaction does not need this permission.",
    requested: "Your decision is required. The Agent cannot grant permission.",
    allowed: "Allowed. Tell the Agent to continue. You can revoke access at any time.",
    denied: "Denied. Normal browsing and page interaction are unaffected.",
    allow: "Allow export", deny: "Deny", revoke: "Revoke access", updating: "Updating permission…",
    failed: "Permission could not be updated. Try again.", updated: "Permission updated.",
  },
} as const;

function renderCookieConsents(): void {
  if (!cookiePanel || !cookieSites) return;
  const copy = COOKIE_COPY[shellLang];
  cookiePanel.hidden = cookieConsents.length === 0;
  const summary = document.getElementById("cookie-summary");
  const explanation = document.getElementById("cookie-explanation");
  if (summary) summary.textContent = copy.title;
  if (explanation) explanation.textContent = copy.explanation;
  cookieSites.replaceChildren();
  for (const consent of cookieConsents) {
    const section = document.createElement("section");
    section.className = "cookie-site";
    const origin = document.createElement("h2");
    origin.className = "cookie-origin";
    origin.textContent = consent.origin;
    const state = document.createElement("p");
    state.className = "cookie-state";
    state.textContent = copy[consent.state];
    const actions = document.createElement("div");
    actions.className = "cookie-actions";
    for (const allow of consent.state === "allowed" ? [false] : consent.state === "denied" ? [true] : [false, true]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "cookie-action";
      button.disabled = cookieDecisionPending;
      button.textContent = allow ? copy.allow : consent.state === "allowed" ? copy.revoke : copy.deny;
      button.setAttribute("aria-label", `${button.textContent}: ${consent.origin}`);
      if (allow) button.dataset.primary = "true";
      button.addEventListener("click", event => {
        // No synthetic approval from iframe scripts or content scripts.
        if (event.isTrusted) void decideCookieConsent(consent.id, allow);
      });
      actions.append(button);
    }
    section.append(origin, state, actions);
    cookieSites.append(section);
  }
}

async function refreshCookieConsents(): Promise<void> {
  const response = await sendToSw<{ ok?: boolean; consents?: CookieConsent[] }>({
    type: "COOKIE_CONSENT_LIST", panelContextId, windowId: currentWindowId,
  });
  if (!response?.ok) return;
  const previous = new Set(cookieConsents.map(item => item.id));
  cookieConsents = response.consents || [];
  if (cookiePanel && cookieConsents.some(item => item.state === "requested" && !previous.has(item.id))) cookiePanel.open = true;
  renderCookieConsents();
}

async function decideCookieConsent(id: string, allow: boolean): Promise<void> {
  if (cookieDecisionPending) return;
  cookieDecisionPending = true;
  if (cookieFeedback) cookieFeedback.textContent = COOKIE_COPY[shellLang].updating;
  renderCookieConsents();
  const response = await sendToSw<{ ok?: boolean; consents?: CookieConsent[] }>({
    type: "COOKIE_CONSENT_DECIDE", consent_id: id, allow, panelContextId, windowId: currentWindowId,
  });
  cookieDecisionPending = false;
  if (response?.ok) cookieConsents = response.consents || [];
  renderCookieConsents();
  if (cookieFeedback) cookieFeedback.textContent = response?.ok ? COOKIE_COPY[shellLang].updated : COOKIE_COPY[shellLang].failed;
}

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
      void refreshCookieConsents();
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
      // HTTP error documents also fire load. Only the app's trusted ready
      // handshake can dismiss the overlay; otherwise retain the retry timer.
      void sendToSw<Binding>({ type: "REQUEST_BINDING", panelContextId, windowId: currentWindowId })
        .then(fresh => { if (fresh) postToIframe({ type: "BINDING", ...fresh }); });
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
    chatId?: string;
    account?: string;
  } | null;
  if (!m?.type) return;

  if (m.type === "EMBED_READY" || m.type === "REQUEST_BINDING") {
    if (loadTimer) clearTimeout(loadTimer);
    hideShellState();
  }

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
    renderCookieConsents();
    renderLocalDownload();
  } else if (m.type === "SET_THEME") {
    applyShellTheme(m.theme);
    void sendToSw({ type: "SET_THEME", theme: shellTheme });
  } else if (m.type === "BROWSER_TURN_CANCELLED") {
    void sendToSw({
      type: "BROWSER_TURN_CANCELLED",
      chat_id: m.chat_id,
      turn_id: m.turn_id,
    });
  } else if (m.type === "PAGE_QUOTE_CONTEXT") {
    quoteContext = m.chatId && m.account ? { chatId: m.chatId, account: m.account } : null;
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
    void sendToSw<{ ok?: boolean; connected?: boolean }>({
      type: "OPEN_WS",
      scopedToken: m.scopedToken,
    }).then((r) => postToIframe({ type: "OPEN_WS_RESULT", ok: !!r?.ok, connected: r?.connected === true }));
  }
});

// Reflect WS connection state into the tiny fallback chip (the iframe is the
// primary UI; this only matters before/around the iframe mounting).
chrome.runtime.onMessage.addListener((msg: unknown, sender, respond) => {
  const m = msg as {
    type?: string;
    chat_id?: string;
    status?: string;
    browser_window_id?: string | number;
    windowId?: number;
  } | null;
  if (m?.type === 'PAGE_QUOTE_CONTEXT_REQUEST' && sender.id === chrome.runtime.id && m.windowId === currentWindowId) {
    respond(quoteContext);
    return false;
  }
  if (m?.type === "WS_OPEN" || m?.type === "WS_CLOSED") {
    postToIframe({ type: "BROWSER_TRANSPORT_STATE", connected: m.type === "WS_OPEN" });
  }
  if (m?.type === "WS_AUTH_REQUIRED") {
    postToIframe({ type: "BROWSER_WS_AUTH_REQUIRED" });
  }
  if (m?.type === "BROWSER_SESSION_CHANGED") {
    void refreshCookieConsents();
    postToIframe(projectBrowserControlForWindow(
      m as unknown as Record<string, unknown>,
      currentWindowId,
    ));
  }
  if (m?.type === "COOKIE_CONSENT_CHANGED") void refreshCookieConsents();
  if (m?.type === "DOWNLOAD_CONFIRM_CHANGED" || m?.type === "BROWSER_SESSION_CHANGED") void refreshLocalDownload();
  if (m?.type === "PAGE_QUOTE" && (m as Record<string, unknown>).windowId === currentWindowId) postToIframe(m);
  if (m?.type === "BROWSER_STOP_REQUESTED") postToIframe(m);
  return false;
});
