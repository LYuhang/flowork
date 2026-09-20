/**
 * The single in-page browser-control surface.
 *
 * The status pill and Stop action live in one closed Shadow DOM. Page-element
 * highlighting is a visually separate, pointer-inert outline in that same
 * shadow root, so it can never become a second control card. This entry has no
 * imports: MV3 content scripts must remain a self-contained classic bundle.
 */

type Kind =
  | "ready"
  | "thinking"
  | "tool"
  | "browser_tool"
  | "streaming"
  | "confirm"
  | "disconnected"
  | "permission_missing"
  | "tab_lost"
  | "stopped"
  | "recovered"
  | "error";

interface IslandState {
  controlled: boolean;
  kind?: Kind;
  tool?: string;
  message?: string;
}

type Lang = "zh" | "en";

const HOST_ID = "flowork-island-host";

const TOOL_LABELS: Record<string, { zh: string; en: string }> = {
  navigate: { zh: "打开网页", en: "Open page" },
  snapshot: { zh: "读取页面", en: "Read page" },
  read_text: { zh: "读取页面", en: "Read page" },
  read_fields: { zh: "读取页面", en: "Read page" },
  click: { zh: "点击", en: "Click" },
  type: { zh: "输入", en: "Type" },
  fill: { zh: "输入", en: "Type" },
  select: { zh: "选择", en: "Select" },
  press: { zh: "按键", en: "Key press" },
  submit: { zh: "提交", en: "Submit" },
  screenshot: { zh: "截图", en: "Screenshot" },
  get_image: { zh: "获取图像", en: "Get image" },
  wait_for: { zh: "等待加载", en: "Waiting" },
  wait: { zh: "等待加载", en: "Waiting" },
  query: { zh: "查找元素", en: "Find element" },
  check_login: { zh: "检查登录", en: "Check login" },
  start_session: { zh: "开启控制", en: "Start control" },
};

const ISLAND_CSS = `
:host { color-scheme: dark; }
.pill {
  position: fixed;
  top: max(10px, env(safe-area-inset-top));
  left: 50%;
  z-index: 2147483647;
  display: flex;
  min-height: 36px;
  max-width: min(560px, calc(100vw - 24px));
  align-items: center;
  gap: 9px;
  box-sizing: border-box;
  padding: 4px 5px 4px 12px;
  border: 1px solid rgba(255,255,255,.16);
  border-radius: 999px;
  background: #1b1b1f;
  color: #f8f8fa;
  font: 500 12px/1.35 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  box-shadow: 0 8px 24px rgba(0,0,0,.28);
  pointer-events: auto;
  transform: translateX(-50%);
  transition: opacity 160ms ease, transform 160ms ease;
  white-space: nowrap;
}
.pill[data-hidden="true"] { opacity: 0; pointer-events: none; transform: translate(-50%,-8px); }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #a3a3aa; flex: none; }
.pill[data-kind="thinking"] .dot,
.pill[data-kind="tool"] .dot,
.pill[data-kind="browser_tool"] .dot,
.pill[data-kind="streaming"] .dot { background: #67a4ff; animation: pulse 1.4s ease-in-out infinite; }
.pill[data-kind="confirm"] .dot { background: #f5bb45; }
.pill[data-kind="disconnected"] .dot,
.pill[data-kind="permission_missing"] .dot,
.pill[data-kind="tab_lost"] .dot,
.pill[data-kind="error"] .dot { background: #ff6b72; }
.pill[data-kind="stopped"] .dot { background: #a3a3aa; }
.pill[data-kind="recovered"] .dot { background: #55c985; }
.label { min-width: 0; max-width: min(420px, 52vw); overflow: hidden; text-overflow: ellipsis; }
.stop {
  all: unset;
  min-width: 32px;
  min-height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  padding: 0 11px;
  border-radius: 999px;
  background: #f1f1f3;
  color: #19191d;
  cursor: pointer;
  font: 650 12px/1 ui-sans-serif, system-ui, sans-serif;
}
.stop:hover { background: #fff; }
.stop:focus-visible { outline: 2px solid #86b7ff; outline-offset: 2px; }
.stop:active, .stop[aria-pressed="true"] { background: #d9d9de; transform: translateY(1px); }
.highlight {
  position: fixed;
  z-index: 2147483646;
  display: none;
  box-sizing: border-box;
  border: 2px solid #5b9dff;
  border-radius: 6px;
  background: rgba(91,157,255,.08);
  box-shadow: 0 0 0 3px rgba(91,157,255,.18);
  pointer-events: none;
}
.highlight[data-visible="true"] { display: block; }
.highlight-label {
  position: absolute;
  left: -2px;
  bottom: calc(100% + 5px);
  max-width: 240px;
  overflow: hidden;
  padding: 3px 7px;
  border-radius: 5px;
  background: #1b1b1f;
  color: #fff;
  font: 600 11px/1.3 ui-sans-serif, system-ui, sans-serif;
  text-overflow: ellipsis;
  white-space: nowrap;
}
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .45; } }
@media (prefers-reduced-motion: reduce) {
  .pill { transition: none; }
  .dot { animation: none !important; }
}
`;

function labelFor(state: IslandState, lang: Lang): string {
  if (state.message) return state.message;
  const kind = state.kind ?? "ready";
  switch (kind) {
    case "ready": return lang === "zh" ? "浏览器正在被控制" : "Browser is being controlled";
    case "thinking": return lang === "zh" ? "Agent 思考中…" : "Agent thinking…";
    case "tool": return lang === "zh" ? "调用工具中…" : "Calling tool…";
    case "streaming": return lang === "zh" ? "Agent 输出中…" : "Agent responding…";
    case "confirm": return lang === "zh" ? "等待你的确认" : "Waiting for your confirmation";
    case "browser_tool": {
      const entry = state.tool ? TOOL_LABELS[state.tool] : undefined;
      const name = entry ? entry[lang] : state.tool || (lang === "zh" ? "浏览器操作" : "browser action");
      return lang === "zh" ? `正在${name}…` : `${name}…`;
    }
    case "disconnected": return lang === "zh" ? "连接已断开，等待恢复" : "Disconnected — waiting to recover";
    case "permission_missing": return lang === "zh" ? "缺少浏览器控制权限" : "Browser-control permission required";
    case "tab_lost": return lang === "zh" ? "受控标签页已丢失" : "Controlled tab was lost";
    case "stopped": return lang === "zh" ? "已请求停止当前任务" : "Stop requested";
    case "recovered": return lang === "zh" ? "浏览器控制已恢复" : "Browser control recovered";
    case "error": return lang === "zh" ? "浏览器控制遇到问题" : "Browser control error";
  }
}

class Island {
  private pill!: HTMLDivElement;
  private label!: HTMLSpanElement;
  private dot!: HTMLSpanElement;
  private stop!: HTMLButtonElement;
  private highlightBox!: HTMLDivElement;
  private highlightLabel!: HTMLSpanElement;
  private highlighted: Element | null = null;
  private updateFrame = 0;
  private state: IslandState | undefined;
  private lang: Lang = "en";

  mount(): void {
    if (document.getElementById(HOST_ID)) return;
    const host = document.createElement("div");
    host.id = HOST_ID;
    host.style.cssText = "position:fixed;inset:0;width:0;height:0;pointer-events:none;z-index:2147483647";
    document.documentElement.appendChild(host);
    const root = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = ISLAND_CSS;

    this.pill = document.createElement("div");
    this.pill.className = "pill";
    this.pill.dataset.hidden = "true";
    this.pill.setAttribute("role", "status");
    this.pill.setAttribute("aria-live", "polite");
    this.dot = document.createElement("span");
    this.dot.className = "dot";
    this.dot.setAttribute("aria-hidden", "true");
    this.label = document.createElement("span");
    this.label.className = "label";
    this.stop = document.createElement("button");
    this.stop.className = "stop";
    this.stop.type = "button";
    this.stop.addEventListener("click", () => {
      this.stop.setAttribute("aria-pressed", "true");
      this.render({ ...(this.state ?? { controlled: true }), controlled: true, kind: "stopped", message: undefined }, this.lang);
      try { chrome.runtime.sendMessage({ type: "STOP" }); } catch { /* service worker unavailable */ }
    });
    this.pill.append(this.dot, this.label, this.stop);

    this.highlightBox = document.createElement("div");
    this.highlightBox.className = "highlight";
    this.highlightBox.setAttribute("aria-hidden", "true");
    this.highlightLabel = document.createElement("span");
    this.highlightLabel.className = "highlight-label";
    this.highlightBox.appendChild(this.highlightLabel);
    root.append(style, this.highlightBox, this.pill);

    const schedulePosition = () => this.scheduleHighlightPosition();
    window.addEventListener("scroll", schedulePosition, true);
    window.addEventListener("resize", schedulePosition);
  }

  render(state: IslandState | undefined, lang: Lang): void {
    this.state = state;
    this.lang = lang;
    this.stop.textContent = lang === "zh" ? "停止" : "Stop";
    this.stop.setAttribute("aria-label", lang === "zh" ? "停止当前 Agent 任务" : "Stop the current agent turn");
    if (!state?.controlled) {
      this.pill.dataset.hidden = "true";
      this.clearHighlight();
      return;
    }
    this.pill.dataset.kind = state.kind ?? "ready";
    this.label.textContent = labelFor(state, lang);
    if (state.kind !== "stopped") this.stop.removeAttribute("aria-pressed");
    this.pill.dataset.hidden = "false";
  }

  narrate(text: string): void {
    if (!this.state?.controlled) return;
    this.render({ ...this.state, message: text.slice(0, 240) }, this.lang);
  }

  highlight(selector: string, label: string): void {
    try { this.highlighted = selector ? document.querySelector(selector) : null; }
    catch { this.highlighted = null; }
    if (!this.highlighted) {
      this.clearHighlight();
      return;
    }
    this.highlightLabel.textContent = label.slice(0, 120);
    this.highlightLabel.hidden = !label;
    this.scheduleHighlightPosition();
  }

  private scheduleHighlightPosition(): void {
    if (!this.highlighted || this.updateFrame) return;
    this.updateFrame = requestAnimationFrame(() => {
      this.updateFrame = 0;
      if (!this.highlighted?.isConnected) return this.clearHighlight();
      const rect = this.highlighted.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return this.clearHighlight();
      Object.assign(this.highlightBox.style, {
        left: `${rect.left - 3}px`, top: `${rect.top - 3}px`,
        width: `${rect.width + 6}px`, height: `${rect.height + 6}px`,
      });
      this.highlightBox.dataset.visible = "true";
    });
  }

  private clearHighlight(): void {
    this.highlighted = null;
    this.highlightBox.dataset.visible = "false";
    if (this.updateFrame) cancelAnimationFrame(this.updateFrame);
    this.updateFrame = 0;
  }
}

const island = new Island();
island.mount();
let lastState: IslandState | undefined;
let lastLang: Lang = "en";
const rerender = () => island.render(lastState, lastLang);

try {
  chrome.storage.session.get("islandState", (result) => {
    void chrome.runtime.lastError;
    lastState = result?.islandState as IslandState | undefined;
    rerender();
  });
  chrome.storage.local.get("lang", (result) => {
    void chrome.runtime.lastError;
    lastLang = result?.lang === "zh" ? "zh" : "en";
    rerender();
  });
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "session" && changes.islandState) {
      lastState = changes.islandState.newValue as IslandState | undefined;
      rerender();
    } else if (area === "local" && changes.lang) {
      lastLang = changes.lang.newValue === "zh" ? "zh" : "en";
      rerender();
    }
  });
  chrome.runtime.onMessage.addListener((message: unknown) => {
    const value = message as { type?: string; text?: string; selector?: string; label?: string } | null;
    if (value?.type === "PAGE_NARRATE") island.narrate(value.text ?? "");
    if (value?.type === "PAGE_HIGHLIGHT") island.highlight(value.selector ?? "", value.label ?? "");
    if (value?.type === "REQUEST_AUTH_SYNC") {
      document.dispatchEvent(new CustomEvent("flowork:extension-auth-refresh"));
    }
    return false;
  });
} catch {
  rerender();
}
