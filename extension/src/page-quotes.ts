/** User-selected page material, independent of debugger ownership. */
type Destination = { chatId: string; account: string };
const destinations = new Map<number, Destination>();
const menuId = 'flowork-quote';

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
  chrome.runtime.onInstalled.addListener(() => {
    chrome.contextMenus.create({ id: menuId, title: 'Quote in Flowork', contexts: ['selection'],
      documentUrlPatterns: ['http://*/*', 'https://*/*'] });
  });
  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (message?.type !== 'PAGE_QUOTE_CONTEXT') return false;
    if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL('sidepanel.html')) return false;
    if (!Number.isInteger(message.windowId)) return false;
    if (typeof message.chatId === 'string' && message.chatId && typeof message.account === 'string' && message.account) {
      destinations.set(message.windowId, { chatId: message.chatId, account: message.account });
    } else destinations.delete(message.windowId);
    respond({ ok: true });
    return false;
  });
  chrome.contextMenus.onClicked.addListener((info, tab) => {
    if (info.menuItemId !== menuId || !tab?.id || !info.selectionText) return;
    const destination = destinations.get(tab.windowId);
    const deliver = (payload: Record<string, unknown>) => chrome.runtime.sendMessage({
      type: 'PAGE_QUOTE', windowId: tab.windowId, ...destination, ...payload,
    }).catch(() => undefined);
    void (async () => {
      if (!destination) {
        await chrome.sidePanel.open({ windowId: tab.windowId });
        await deliver({ error: 'Open a conversation in this window, then select the text and quote it again.' });
        return;
      }
      const text = info.selectionText!;
      if (text.length > 32768) { await deliver({ error: 'Select at most 32768 characters for a quote.' }); return; }
      const target = (await chrome.debugger.getTargets()).find(target => target.tabId === tab.id);
      if (!target) { await deliver({ error: 'The selected tab is no longer available. Select the text again.' }); return; }
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
