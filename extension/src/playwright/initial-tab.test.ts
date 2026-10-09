import { describe, expect, it, vi } from "vitest";
import { initialControlTab } from "./initial-tab";

function fixture(url: string, pendingUrl?: string) {
  const active = { id: 10, windowId: 7, url, pendingUrl } as chrome.tabs.Tab;
  const blank = { id: 11, windowId: 7, url: "about:blank" } as chrome.tabs.Tab;
  const api = { query: vi.fn(async () => [active]), create: vi.fn(async () => blank) };
  return { api, active, blank };
}

describe("initial browser control tab", () => {
  it.each(["chrome://newtab/", "chrome://new-tab-page/"])("bootstraps %s in the originating window", async url => {
    const { api, blank } = fixture(url);
    expect(await initialControlTab(api, 7)).toEqual(blank);
    expect(api.query).toHaveBeenCalledWith({ active: true, windowId: 7 });
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(api.create).toHaveBeenCalledWith({ windowId: 7, url: "about:blank", active: true });
  });
  it.each(["about:blank", "https://example.com", "file:///example.txt"])("reuses an allowed page %s", async url => {
    const { api, active } = fixture(url);
    expect(await initialControlTab(api, 7)).toEqual(active);
    expect(api.create).not.toHaveBeenCalled();
  });
  it.each(["chrome://settings/", "chrome://extensions/", "chrome-extension://id/page.html", "devtools://devtools/", "javascript:alert(1)"])("does not bootstrap restricted page %s", async url => {
    const { api } = fixture(url);
    await expect(initialControlTab(api, 7)).rejects.toThrow("cannot be controlled");
    expect(api.create).not.toHaveBeenCalled();
  });
  it("does not adopt an active tab from another window", async () => {
    const { api } = fixture("chrome://newtab/");
    await expect(initialControlTab(api, 8)).rejects.toThrow("No active page");
    expect(api.create).not.toHaveBeenCalled();
  });
  it("checks pending navigation before accepting the old page URL", async () => {
    const { api } = fixture("https://example.com", "chrome://settings/");
    await expect(initialControlTab(api, 7)).rejects.toThrow("cannot be controlled");
  });
});
