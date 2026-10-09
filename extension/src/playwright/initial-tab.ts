import type { RelayTab } from "./relay-executor";

export function isControllableInitialUrl(url?: string): boolean {
  return url === "about:blank" || /^(https?|file):/.test(url || "");
}

/** Bootstrap an authorized window without exposing Chrome's new-tab UI. */
export async function initialControlTab(
  tabs: Pick<typeof chrome.tabs, "query" | "create">,
  windowId: number,
): Promise<RelayTab> {
  const [active] = await tabs.query({ active: true, windowId });
  if (active?.id === undefined || active.windowId !== windowId)
    throw new Error("No active page is available in the side-panel window");
  const url = active.pendingUrl || active.url;
  if (isControllableInitialUrl(url)) return active;
  if (url === "chrome://newtab/" || url === "chrome://new-tab-page/") {
    // Create a separate inert page; never navigate or inspect browser UI.
    return tabs.create({ windowId, url: "about:blank", active: true });
  }
  throw new Error("Browser and extension UI cannot be controlled by the Agent. Open a web page or a Chrome new tab first.");
}
