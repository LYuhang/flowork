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


describe('offscreen worker wakeups', () => {
  it('keeps socket heartbeats local without a worker keepalive port', async () => {
    const open = await setup();
    await open('token');
    const socket = Socket.instances[0]; socket.open();
    vi.mocked(chrome.runtime.sendMessage).mockClear();
    for (let i = 0; i < 20; i++) {
      await vi.advanceTimersByTimeAsync(15_000);
      const ping = JSON.parse(socket.sent.at(-1)!);
      expect(ping.kind).toBe('ping');
      expect(ping.data).toEqual({ type: 'keepalive' });
      socket.echo(ping.data, ping.id);
    }
    expect(socket.sent).toHaveLength(20);
    expect(socket.readyState).toBe(Socket.OPEN);
    expect(chrome.runtime.connect).not.toHaveBeenCalled();
    expect(chrome.runtime.sendMessage).not.toHaveBeenCalled();
  });

  it('delivers lifecycle acknowledgement data to the worker for pending-event cleanup', async () => {
    const open = await setup();
    await open('token');
    const socket = Socket.instances[0]; socket.open();
    vi.mocked(chrome.runtime.sendMessage).mockClear();
    const ack = { type: 'browser_session_event_ack', browser_session_id: 'session',
      session_generation: 2, event_seq: 3 };
    socket.echo(ack, 'terminal-event');
    expect(chrome.runtime.sendMessage).toHaveBeenCalledTimes(1);
    expect(chrome.runtime.sendMessage).toHaveBeenCalledWith({ type: 'WS_ECHO', echo: ack });
  });

  it('still wakes the worker for a new browser relay frame', async () => {
    const open = await setup();
    await open('token');
    const socket = Socket.instances[0]; socket.open();
    vi.mocked(chrome.runtime.sendMessage).mockClear();
    const env = { v: 1, kind: 'playwright_relay', id: 'command',
      channel: 'chat:one', transport: 'browser', data: { method: 'Target.getTargets' } };
    socket.onmessage?.({ data: JSON.stringify(env) } as MessageEvent);
    expect(chrome.runtime.sendMessage).toHaveBeenCalledWith({
      type: 'PLAYWRIGHT_RELAY_FRAME', env,
    }, expect.any(Function));
  });
});

it('returns a correlated CDP error when the service worker messaging fails', async () => {
 const open = await setup(); await open('token');
 const socket = Socket.instances[0]; socket.open();
 const env = { v: 1, kind: 'playwright_relay', id: 'outer', channel: 'chat:one', transport: 'browser',
   data: { action: 'request', request: { id: 73, sessionId: 'page', method: 'Flowork.tabInfo' } } };
 socket.onmessage?.({ data: JSON.stringify(env) } as MessageEvent);
 const call = vi.mocked(chrome.runtime.sendMessage).mock.calls.find(args => (args[0] as unknown as { type?: string })?.type === 'PLAYWRIGHT_RELAY_FRAME')!;
 Object.defineProperty(chrome.runtime, 'lastError', { configurable: true, value: { message: 'worker channel closed' } });
 (call[1] as (value?: unknown) => void)();
 const response = JSON.parse(socket.sent.at(-1)!);
 expect(response.data.message).toEqual({ id: 73, sessionId: 'page', error: { code: -32603, message: 'worker channel closed' } });
});

it('does not forward a late old command reply into a replacement socket', async () => {
 const open = await setup(); await open('token');
 const socket = Socket.instances[0]; socket.open();
 socket.onmessage?.({ data: JSON.stringify({ v: 1, kind: 'playwright_relay', id: 'old', channel: 'chat:one', transport: 'browser', data: {} }) } as MessageEvent);
 const call = vi.mocked(chrome.runtime.sendMessage).mock.calls.find(args => (args[0] as unknown as { type?: string })?.type === 'PLAYWRIGHT_RELAY_FRAME')!;
 socket.close(1006); await vi.advanceTimersByTimeAsync(1000);
 const next = Socket.instances[1]; next.open();
 (call[1] as (value: unknown) => void)('old-reply');
 expect(next.sent).not.toContain('old-reply');
 expect(Socket.instances).toHaveLength(2);
});
