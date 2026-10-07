/** A conversation's browser window comes from its sending panel, never focus. */
export type ChatOrigin = { windowId: number; panelContextId: string };
const prefix = 'browserChatOrigin:';
const writes = new Map<string, Promise<void>>();

export async function chatOrigin(chatId: string): Promise<ChatOrigin | null> {
  await writes.get(chatId);
  return (await chrome.storage.session.get(prefix + chatId))[prefix + chatId] ?? null;
}

export async function clearChatOrigins(): Promise<void> {
  await Promise.allSettled(writes.values());
  const saved = await chrome.storage.session.get(null);
  await chrome.storage.session.remove(Object.keys(saved).filter(key => key.startsWith(prefix)));
}

export function registerChatOrigins(): void {
  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (message?.type !== 'BROWSER_TURN_ORIGIN') return false;
    if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL('sidepanel.html')) return false;
    if (typeof message.chatId !== 'string' || !message.chatId || message.chatId.length > 128
      || !Number.isInteger(message.windowId) || message.windowId < 0 || typeof message.panelContextId !== 'string') return false;
    const chatId = message.chatId;
    const saved = chrome.storage.session.set({ [prefix + chatId]: {
      windowId: message.windowId, panelContextId: message.panelContextId,
    } });
    writes.set(chatId, saved);
    void saved.then(() => respond({ ok: true }), () => respond({ ok: false })).finally(() => {
      if (writes.get(chatId) === saved) writes.delete(chatId);
    });
    return true;
  });
}
