import { afterEach, expect, it, vi } from 'vitest';
import { chatOrigin, clearChatOrigins, registerChatOrigins } from './chat-origins';

afterEach(() => vi.unstubAllGlobals());

it('keeps each sending window across focus changes and a worker reload', async () => {
  const saved: Record<string, unknown> = {};
  let receive: (message: unknown, sender: unknown, respond: unknown) => unknown = () => {};
  vi.stubGlobal('chrome', {
    runtime: { id: 'ext', getURL: (path: string) => `chrome-extension://ext/${path}`,
      onMessage: { addListener: (handler: typeof receive) => { receive = handler; } } },
    storage: { session: {
      get: vi.fn(async (key: string | null) => key ? { [key]: saved[key] } : saved),
      set: vi.fn(async (items: Record<string, unknown>) => { Object.assign(saved, items); }),
      remove: vi.fn(async (keys: string[]) => { for (const key of keys) delete saved[key]; }),
    } },
  });
  registerChatOrigins();
  const sender = { id: 'ext', url: 'chrome-extension://ext/sidepanel.html' };
  receive({ type: 'BROWSER_TURN_ORIGIN', chatId: 'a', windowId: 7, panelContextId: 'panel-a' }, sender, vi.fn());
  receive({ type: 'BROWSER_TURN_ORIGIN', chatId: 'b', windowId: 8, panelContextId: 'panel-b' }, sender, vi.fn());
  saved.activePanelWindowId = 8;
  expect(await chatOrigin('a')).toEqual({ windowId: 7, panelContextId: 'panel-a' });
  expect(await chatOrigin('b')).toEqual({ windowId: 8, panelContextId: 'panel-b' });
  expect(receive({ type: 'BROWSER_TURN_ORIGIN', chatId: 'a', windowId: 99, panelContextId: 'fake' },
    { id: 'ext', url: 'https://example.com' }, vi.fn())).toBe(false);
  vi.resetModules();
  const restored = await import('./chat-origins');
  expect(await restored.chatOrigin('a')).toEqual({ windowId: 7, panelContextId: 'panel-a' });
  expect(await restored.chatOrigin('missing')).toBeNull();
  await clearChatOrigins();
  expect(await restored.chatOrigin('a')).toBeNull();
  expect(saved.activePanelWindowId).toBe(8);
});
