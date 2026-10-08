import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

class Socket {
  static OPEN = 1;
  static CONNECTING = 0;
  static instances: Socket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  sent: string[] = [];
  constructor(public url: string, public protocols: string[]) { Socket.instances.push(this); }
  send(raw: string) { this.sent.push(raw); }
  open() { this.readyState = 1; this.onopen?.(); }
  close(code = 1000) { this.readyState = 3; this.onclose?.({ code } as CloseEvent); }
  echo(data: object, id = 'auth') {
    this.onmessage?.({ data: JSON.stringify({ v: 1, kind: 'echo', id,
      channel: 'system', transport: 'browser', data }) } as MessageEvent);
  }
}

beforeEach(() => {
  vi.resetModules();
  vi.useFakeTimers();
  Socket.instances = [];
  vi.stubGlobal('WebSocket', Socket);
  vi.stubGlobal('chrome', { runtime: {
    connect: vi.fn(() => ({ postMessage: vi.fn(), disconnect: vi.fn(),
      onDisconnect: { addListener: vi.fn() } })),
    onMessage: { addListener: vi.fn() },
    sendMessage: vi.fn(async () => undefined),
  } });
});
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); });

async function setup() {
  await import('./offscreen');
  const listener = vi.mocked(chrome.runtime.onMessage.addListener).mock.calls[0][0];
  return (token: string) => new Promise<unknown>(resolve => listener({
    type: 'OPEN_WS_INTERNAL', wsBase: 'wss://example.test', token, browser: 'browser-1',
  }, {}, resolve));
}

describe('offscreen authentication lifecycle', () => {
  it('reopens with a fresh token after renewal is missed while the sidebar is closed', async () => {
    const open = await setup();
    expect(await open('old.token')).toMatchObject({ ok: true });
    const old = Socket.instances[0]; old.open();
    old.echo({ type: 'auth_status', expires_at: Date.now() / 1000 + 900 });
    // Keep the transport responsive while no sidebar services the renewal.
    for (let i = 0; i < 60; i++) {
      old.echo({});
      await vi.advanceTimersByTimeAsync(15_000);
    }
    expect(chrome.runtime.sendMessage).toHaveBeenCalledWith({ type: 'WS_AUTH_REQUIRED' });
    old.close(4401);
    await vi.advanceTimersByTimeAsync(120_000);
    expect(Socket.instances).toHaveLength(1); // no expired-credential reconnect loop
    expect(await open('fresh.token')).toMatchObject({ ok: true });
    const current = Socket.instances[1]; current.open();
    expect(current.protocols).toContain('vibecanvas.browser.auth.fresh.token');
    vi.mocked(chrome.runtime.sendMessage).mockClear();
    old.onclose?.({ code: 4401 } as CloseEvent);
    old.echo({ type: 'auth_status', expires_at: Date.now() / 1000 + 10 });
    expect(chrome.runtime.sendMessage).not.toHaveBeenCalled();
    expect(current.sent).toEqual([]); // no old relay/action replay
    expect(await open('fresh.token')).toEqual({ ok: true, reused: true, connected: true });
  });

  it('replaces a socket when auth renewal receives no acknowledgement, without replaying its frames', async () => {
    const open = await setup();
    await open('old.token');
    const old = Socket.instances[0]; old.open();
    const pending = open('new.token');
    expect(Socket.instances).toHaveLength(1);
    const request = JSON.parse(old.sent[0]);
    expect(request.kind).toBe('auth_refresh');
    await vi.advanceTimersByTimeAsync(5000);
    expect(await pending).toMatchObject({ ok: true });
    expect(old.readyState).toBe(3);
    expect(Socket.instances).toHaveLength(2);
    const current = Socket.instances[1]; current.open();
    old.echo({ type: 'auth_refresh', ok: true }, request.id);
    expect(current.protocols).toContain('vibecanvas.browser.auth.new.token');
    expect(current.sent).toEqual([]);
    expect(await open('new.token')).toEqual({ ok: true, reused: true, connected: true });
  });
});
