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
