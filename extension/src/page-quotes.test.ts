// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { capturePageSelection } from './page-quotes';
beforeEach(() => {
  document.body.innerHTML = '<section><p>Before <strong>selected words</strong> after</p></section>';
  vi.stubGlobal('CSS', { escape: (value: string) => value });
});
afterEach(() => { window.getSelection()?.removeAllRanges(); vi.unstubAllGlobals(); });
describe('web quote selection', () => {
  it('captures a usable element path and selected text without debugger control', () => {
    const range = document.createRange(); range.selectNodeContents(document.querySelector('strong')!);
    window.getSelection()!.addRange(range);
    const quote = capturePageSelection()!;
    expect(quote.text).toBe('selected words');
    expect(document.querySelector(quote.css_selector)).toBe(document.querySelector('strong'));
    expect(quote.frame_url).toBe(location.href);
  });
  it('retains prefix and suffix around a partial text selection', () => {
    const text = document.querySelector('strong')!.firstChild!;
    const range = document.createRange(); range.setStart(text, 2); range.setEnd(text, 8);
    window.getSelection()!.addRange(range);
    expect(capturePageSelection()).toMatchObject({ text: 'lected', prefix: 'se', suffix: ' words' });
  });
  it('does not fabricate an element path without a selection', () => {
    expect(capturePageSelection()).toBeNull();
  });
});

it('uses CLI target IDs and the source window destination without attaching debugger', async () => {
  vi.resetModules();
  let click!: (info: unknown, tab: unknown) => void;
  let message!: (data: unknown, sender: unknown, reply: unknown) => void;
  const send = vi.fn(async () => ({}));
  const attach = vi.fn();
  vi.stubGlobal('chrome', {
    runtime: { id: 'extension', getURL: (path: string) => `chrome-extension://extension/${path}`,
      onInstalled: { addListener: vi.fn() },
      onMessage: { addListener: (handler: typeof message) => { message = handler; } }, sendMessage: send },
    contextMenus: { update: vi.fn(), create: vi.fn(), onClicked: { addListener: (handler: typeof click) => { click = handler; } } },
    debugger: { attach, getTargets: vi.fn(async () => [{ id: 'target-23', tabId: 23 }]) },
    scripting: { executeScript: vi.fn(async () => [{ result: { text: 'selected', css_selector: '#answer', prefix: 'before', suffix: 'after' } }]) },
    sidePanel: { open: vi.fn(async () => undefined) },
  });
  const { registerPageQuotes } = await import('./page-quotes');
  registerPageQuotes();
  message({ type: 'PAGE_QUOTE_CONTEXT', windowId: 7, chatId: 'chat', account: 'owner' },
    { id: 'extension', url: 'chrome-extension://extension/sidepanel.html' }, vi.fn());
  // A page cannot overwrite the selected destination.
  message({ type: 'PAGE_QUOTE_CONTEXT', windowId: 7, chatId: 'attacker', account: 'other' },
    { id: 'extension', url: 'https://example.com' }, vi.fn());
  click({ menuItemId: 'flowork-quote', selectionText: 'selected', pageUrl: 'https://example.com', frameId: 0 },
    { id: 23, windowId: 7, title: 'Report', url: 'https://example.com' });
  await vi.waitFor(() => expect(send).toHaveBeenCalled());
  expect(send).toHaveBeenCalledWith(expect.objectContaining({ windowId: 7, chatId: 'chat', account: 'owner',
    attachment: expect.objectContaining({ type: 'quote', snapshot: { text: 'selected' },
      selector: expect.objectContaining({ tab_id: 'tab_target-23', window_id: 'win_7', css_selector: '#answer' }) }) }));
  expect(attach).not.toHaveBeenCalled();
});

it.each([false, true])('ensures the menu at worker startup (missing=%s)', async (missing) => {
  const create = vi.fn();
  const update = vi.fn((_id, _properties, callback) => callback());
  vi.stubGlobal('chrome', {
    runtime: { lastError: missing ? { message: 'Cannot find menu item' } : undefined,
      onMessage: { addListener: vi.fn() } },
    contextMenus: { update, create, onClicked: { addListener: vi.fn() } },
  });
  const { registerPageQuotes } = await import('./page-quotes');
  registerPageQuotes();
  expect(update).toHaveBeenCalledWith('flowork-quote', expect.objectContaining({ contexts: ['selection'] }), expect.any(Function));
  expect(create).toHaveBeenCalledTimes(missing ? 1 : 0);
});
