/** User-selected page material, independent of debugger ownership. */
type Destination = { chatId: string; account: string };
const menuId = 'flowork-quote';
type QuotedTab = { tabId: number; windowId: number; targetId: string };
const quoteKey = (chatId: string) => `pageQuoteTabs:${chatId}:`;

export async function clearQuotedTabs(): Promise<void> {
  const saved = await chrome.storage.session.get(null);
  await chrome.storage.session.remove(Object.keys(saved).filter(key => key.startsWith('pageQuoteTabs:')));
}

/** Only native user quotes create these tab grants; attachment JSON cannot. */
export async function quotedTabsForChat(chatId: string): Promise<chrome.tabs.Tab[]> {
  const key = quoteKey(chatId);
  const saved = await chrome.storage.session.get(null);
  const records = Object.entries(saved).filter(([name]) => name.startsWith(key)).map(([, value]) => value as QuotedTab);
  if (!records.length) return [];
  const targets = await chrome.debugger.getTargets();
  const tabs: chrome.tabs.Tab[] = [];
  for (const record of records) {
    if (!targets.some(target => target.tabId === record.tabId && target.id === record.targetId)) continue;
    try {
      const tab = await chrome.tabs.get(record.tabId);
      if (tab.windowId === record.windowId) tabs.push(tab);
    } catch { /* Closed tabs are not replaced with a different page. */ }
  }
  return tabs;
}

export function capturePageSelection() {
  const selection = window.getSelection();
  if (!selection?.rangeCount || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  const element = range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE
    ? range.commonAncestorContainer as Element : range.commonAncestorContainer.parentElement;
  if (!element) return null;
  const parts: string[] = [];
  let current: Element | null = element;
  while (current && parts.join(' > ').length < 1900) {
    if (current.id) { parts.unshift(`#${CSS.escape(current.id)}`); break; }
    const tag = current.localName;
    const siblings = current.parentElement ? Array.from(current.parentElement.children).filter(sibling => sibling.localName === tag) : [current];
    parts.unshift(`${tag}:nth-of-type(${siblings.indexOf(current) + 1})`);
    current = current.parentElement;
  }
  const before = document.createRange(); before.selectNodeContents(element); before.setEnd(range.startContainer, range.startOffset);
  const after = document.createRange(); after.selectNodeContents(element); after.setStart(range.endContainer, range.endOffset);
  return { text: selection.toString(), css_selector: parts.join(' > '),
    prefix: before.toString().slice(-512), suffix: after.toString().slice(0, 512), frame_url: location.href };
}

export function registerPageQuotes() {
  // Menus survive worker suspension, but an existing installation may not have
  // this menu yet. Ensure it on every worker start, without duplicating it.
  const properties = { title: 'Quote in Flowork', contexts: ['selection'],
    documentUrlPatterns: ['http://*/*', 'https://*/*'] } satisfies chrome.contextMenus.UpdateProperties;
  chrome.contextMenus.update(menuId, properties, () => {
    if (chrome.runtime.lastError) chrome.contextMenus.create({ id: menuId, ...properties });
  });
  chrome.contextMenus.onClicked.addListener((info, tab) => {
    if (info.menuItemId !== menuId || !tab?.id || !info.selectionText) return;
    let destination: Destination | undefined;
    const deliver = (payload: Record<string, unknown>) => chrome.runtime.sendMessage({
      type: 'PAGE_QUOTE', windowId: tab.windowId, ...destination, ...payload,
    }).catch(() => undefined);
    void (async () => {
      // Ask the live shell, rather than retaining a stale destination when the
      // panel closes or losing it when this disposable worker restarts.
      const context = await chrome.runtime.sendMessage({ type: 'PAGE_QUOTE_CONTEXT_REQUEST', windowId: tab.windowId }).catch(() => null);
      if (context?.chatId && context?.account) destination = context;
      if (!destination) {
        await chrome.sidePanel.open({ windowId: tab.windowId });
        await deliver({ error: 'Open a conversation in this window, then select the text and quote it again.' });
        return;
      }
      const text = info.selectionText!;
      if (text.length > 32768) { await deliver({ error: 'Select at most 32768 characters for a quote.' }); return; }
      const target = (await chrome.debugger.getTargets()).find(target => target.tabId === tab.id);
      if (!target) { await deliver({ error: 'The selected tab is no longer available. Select the text again.' }); return; }
      // Independent keys avoid lost updates when two windows quote concurrently.
      await chrome.storage.session.set({ [quoteKey(destination.chatId) + tab.id]:
        { tabId: tab.id, windowId: tab.windowId, targetId: target.id } });
      let selection: ReturnType<typeof capturePageSelection> = null;
      try {
        const results = await chrome.scripting.executeScript({ target: { tabId: tab.id!, frameIds: [info.frameId ?? 0] }, func: capturePageSelection });
        selection = results[0]?.result ?? null;
      } catch { /* Restricted documents still provide the native selection and target identity. */ }
      const sourceUrl = info.pageUrl || tab.url || '';
      const attachment = { schema_version: 1, id: crypto.randomUUID(), type: 'quote',
        label: (tab.title || 'Page quote').slice(0, 512), source: { kind: 'web', url: sourceUrl },
        snapshot: { text }, selector: { kind: 'web_selection', tab_id: `tab_${target.id}`, window_id: `win_${tab.windowId}`,
          title: (tab.title || '').slice(0, 512), frame_url: info.frameUrl || sourceUrl,
          ...(selection?.text === text ? { css_selector: selection.css_selector, prefix: selection.prefix, suffix: selection.suffix } : {}) } };
      // A quote is only data. It never attaches debugger, transfers a lease or sends an Agent turn.
      await deliver({ attachment });
    })().catch(() => deliver({ error: 'Could not capture this quote. Please select the text again.' }));
  });
}
