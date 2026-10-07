// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

beforeEach(() => {
  vi.resetModules();
  vi.useFakeTimers();
  document.body.innerHTML = '<div id="shell-status"><span id="status-title"></span><span id="status-detail"></span><button id="retry" hidden></button></div><iframe id="embed"></iframe>';
  vi.stubGlobal('chrome', {
    runtime: {
      sendMessage: vi.fn((_message, callback) => callback?.({ webBase: 'http://localhost:9001' })),
      onMessage: { addListener: vi.fn() },
    },
    storage: { local: { get: vi.fn(async () => ({})) } },
    windows: { getCurrent: vi.fn(async () => ({ id: 1 })) },
  });
});

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('side-panel app readiness', () => {
  it('records the shell window rather than accepting a window supplied by the iframe', async () => {
    await import('./sidepanel');
    await vi.advanceTimersByTimeAsync(1);
    const frame = document.getElementById('embed') as HTMLIFrameElement;
    const send = (origin: string) => window.dispatchEvent(new MessageEvent('message', {
      source: frame.contentWindow, origin,
      data: { type: 'BROWSER_TURN_ORIGIN', chatId: 'chat', windowId: 999, panelContextId: 'spoofed' },
    }));
    vi.mocked(chrome.runtime.sendMessage).mockClear();
    send('https://untrusted.example');
    expect(chrome.runtime.sendMessage).not.toHaveBeenCalled();
    send('http://localhost:9001');
    expect(chrome.runtime.sendMessage).toHaveBeenCalledWith(expect.objectContaining({
      type: 'BROWSER_TURN_ORIGIN', chatId: 'chat', windowId: 1,
    }), expect.any(Function));
    expect(vi.mocked(chrome.runtime.sendMessage).mock.calls[0][0]).not.toMatchObject({ panelContextId: 'spoofed' });
  });
  it('returns the live quote destination only to its own window and trusted app frame', async () => {
    chrome.runtime.id = 'extension';
    await import('./sidepanel');
    await vi.advanceTimersByTimeAsync(1);
    const frame = document.getElementById('embed') as HTMLIFrameElement;
    const sendContext = (origin: string, chatId: string | null) => window.dispatchEvent(new MessageEvent('message', {
      source: frame.contentWindow, origin, data: { type: 'PAGE_QUOTE_CONTEXT', chatId, account: 'owner' },
    }));
    sendContext('http://localhost:9001', 'chat');
    sendContext('https://untrusted.example', 'other-chat');
    const receive = vi.mocked(chrome.runtime.onMessage.addListener).mock.calls[0][0];
    const reply = vi.fn();
    receive({ type: 'PAGE_QUOTE_CONTEXT_REQUEST', windowId: 2 }, { id: 'extension' }, reply);
    expect(reply).not.toHaveBeenCalled();
    receive({ type: 'PAGE_QUOTE_CONTEXT_REQUEST', windowId: 1 }, { id: 'extension' }, reply);
    expect(reply).toHaveBeenLastCalledWith({ chatId: 'chat', account: 'owner' });
    sendContext('http://localhost:9001', null);
    receive({ type: 'PAGE_QUOTE_CONTEXT_REQUEST', windowId: 1 }, { id: 'extension' }, reply);
    expect(reply).toHaveBeenLastCalledWith(null);
  });
  it('keeps a retry path when an HTTP error document fires iframe load', async () => {
    await import('./sidepanel');
    await vi.advanceTimersByTimeAsync(1);
    document.getElementById('embed')!.dispatchEvent(new Event('load'));
    await vi.advanceTimersByTimeAsync(12_000);
    expect(document.getElementById('shell-status')!.hidden).toBe(false);
    expect(document.getElementById('retry')!.hidden).toBe(false);
  });

  it('only accepts readiness from the expected app iframe and origin', async () => {
    await import('./sidepanel');
    await vi.advanceTimersByTimeAsync(1);
    const frame = document.getElementById('embed') as HTMLIFrameElement;
    window.dispatchEvent(new MessageEvent('message', {
      source: frame.contentWindow, origin: 'https://untrusted.example', data: { type: 'EMBED_READY' },
    }));
    expect(document.getElementById('shell-status')!.hidden).toBe(false);
    window.dispatchEvent(new MessageEvent('message', {
      source: frame.contentWindow, origin: 'http://localhost:9001', data: { type: 'EMBED_READY' },
    }));
    await vi.advanceTimersByTimeAsync(12_000);
    expect(document.getElementById('shell-status')!.hidden).toBe(true);
  });
});
