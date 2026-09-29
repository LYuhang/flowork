import { afterEach, describe, it, expect, vi } from "vitest";
import { BROWSER_WS_PROTOCOL, browserWsProtocols } from "./browser-ws-auth";
import { backoffMs, WsClient } from "./ws-client";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("reconnect backoff", () => {
  it("starts at 1s and doubles per attempt", () => {
    expect(backoffMs(0)).toBe(1000);
    expect(backoffMs(1)).toBe(2000);
    expect(backoffMs(2)).toBe(4000);
    expect(backoffMs(3)).toBe(8000);
    expect(backoffMs(4)).toBe(16000);
  });

  it("caps at 30s", () => {
    expect(backoffMs(5)).toBe(30000); // 32000 would exceed the cap
    expect(backoffMs(10)).toBe(30000);
    expect(backoffMs(100)).toBe(30000);
  });
});

describe("credential-safe WebSocket handshake", () => {
  it("requests renewal before server expiry without disconnecting pending work", async () => {
    vi.useFakeTimers();
    let socket: FakeWebSocket;
    class FakeWebSocket {
      static OPEN = 1;
      static CONNECTING = 0;
      readyState = 1;
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      close = vi.fn(); send = vi.fn();
      constructor() { socket = this; }
    }
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const client = new WsClient("wss://app.example/ws", ["token"]);
    const renew = vi.fn(); client.onAuthRequired(renew); client.connect();
    socket!.onmessage!({ data: JSON.stringify({ v: 1, kind: "echo", id: "browser_auth", channel: "system", transport: "t",
      data: { type: "auth_status", expires_at: Date.now() / 1000 + 120 } }) } as MessageEvent);
    await vi.advanceTimersByTimeAsync(59000); expect(renew).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1000); expect(renew).toHaveBeenCalledTimes(1);
    expect(socket!.close).not.toHaveBeenCalled(); expect(client.isActive()).toBe(true);
    client.disconnect();
  });
  it("renews credentials only after server acknowledgement without opening another socket", async () => {
    const sockets: FakeWebSocket[] = [];
    class FakeWebSocket {
      static OPEN = 1;
      static CONNECTING = 0;
      readyState = 1;
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      sent: string[] = [];
      constructor(public url: string, public protocols: string[]) { sockets.push(this); }
      send(raw: string) { this.sent.push(raw); }
      close() { this.readyState = 3; }
    }
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const client = new WsClient("wss://app.example/ws", ["old"]); client.connect();
    const pending = client.refreshAuthentication("new-token", ["new"]);
    expect(sockets).toHaveLength(1);
    const frame = JSON.parse(sockets[0].sent[0]);
    expect(frame.kind).toBe("auth_refresh");
    expect(frame.data).toEqual({ token: "new-token" });
    sockets[0].onmessage!({ data: JSON.stringify({ ...frame, kind: "echo", data: { type: "auth_refresh", ok: true } }) } as MessageEvent);
    expect(await pending).toBe(true);
    expect(sockets).toHaveLength(1);
    client.disconnect(); client.connect();
    expect(sockets[1].protocols).toEqual(["new"]);
    const cancelled = client.refreshAuthentication("other-user", ["other"]);
    client.disconnect(); expect(await cancelled).toBe(false);
    client.connect(); expect(sockets[2].protocols).toEqual(["new"]);
    client.disconnect();
  });
  it("keeps the scoped credential and browser id out of the URL", () => {
    const calls: unknown[][] = [];
    class FakeWebSocket {
      static OPEN = 1;
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onclose: (() => void) | null = null;
      readyState = 0;
      constructor(...args: unknown[]) {
        calls.push(args);
      }
      close() {}
      send() {}
    }
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const token = "payload.signature";
    const browser = "browser-1";
    const protocols = browserWsProtocols(token, browser);
    new WsClient("wss://app.example/api/v1/browser/ws", protocols).connect();

    expect(calls).toEqual([[
      "wss://app.example/api/v1/browser/ws",
      protocols,
    ]]);
    expect(String(calls[0][0])).not.toContain(token);
    expect(protocols).toEqual([
      BROWSER_WS_PROTOCOL,
      `vibecanvas.browser.auth.${token}`,
      `vibecanvas.browser.id.${browser}`,
    ]);
  });

  it("rejects values that cannot be represented as WebSocket protocol tokens", () => {
    expect(() => browserWsProtocols("token with spaces", "browser-1")).toThrow();
    expect(() => browserWsProtocols("valid.token", "browser:1")).toThrow();
    expect(() => browserWsProtocols("", "browser-1")).toThrow();
  });

  it("requests a fresh capability instead of reconnecting an expired token", () => {
    vi.useFakeTimers();
    class FakeWebSocket {
      static OPEN = 1;
      static instances: FakeWebSocket[] = [];
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      readyState = 0;
      constructor() {
        FakeWebSocket.instances.push(this);
      }
      close() {}
      send() {}
    }
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const client = new WsClient("wss://app.example/api/v1/browser/ws", [
      BROWSER_WS_PROTOCOL,
    ]);
    const refresh = vi.fn();
    client.onAuthRequired(refresh);
    client.connect();
    FakeWebSocket.instances[0].onclose?.({ code: 4401 } as CloseEvent);
    vi.runAllTimers();

    expect(refresh).toHaveBeenCalledOnce();
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});

describe("Playwright relay dispatch", () => {
  it("dispatches relay frames through the only browser-control data plane", () => {
    class FakeWebSocket {
      static OPEN = 1;
      static instance: FakeWebSocket;
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      readyState = 1;
      constructor() {
        FakeWebSocket.instance = this;
      }
      close() {}
      send() {}
    }
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const client = new WsClient("wss://app.example/api/v1/browser/ws");
    const relay = vi.fn();
    client.onPlaywrightRelay(relay);
    client.connect();
    FakeWebSocket.instance.onmessage?.({
      data: JSON.stringify({
        v: 1,
        kind: "playwright_relay",
        id: "pw_1",
        channel: "chat:1",
        transport: "browser:1",
        data: { action: "request" },
        producer: null,
      }),
    } as MessageEvent);

    expect(relay).toHaveBeenCalledOnce();
  });

  it("queues relay responses while the socket is connecting and flushes on open", () => {
    const sent: string[] = [];
    class FakeWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      static instance: FakeWebSocket;
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      readyState = FakeWebSocket.CONNECTING;
      constructor() {
        FakeWebSocket.instance = this;
      }
      close() {}
      send(raw: string) {
        sent.push(raw);
      }
    }
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const client = new WsClient("wss://app.example/api/v1/browser/ws");
    client.connect();

    expect(client.sendRaw("relay-response")).toBe(true);
    expect(sent).toEqual([]);

    FakeWebSocket.instance.readyState = FakeWebSocket.OPEN;
    FakeWebSocket.instance.onopen?.();
    expect(sent).toEqual(["relay-response"]);
  });

  it("does not resurrect a disconnected client from an old reconnect timer", () => {
    vi.useFakeTimers();
    class FakeWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      static instances: FakeWebSocket[] = [];
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      readyState = FakeWebSocket.CONNECTING;
      constructor() {
        FakeWebSocket.instances.push(this);
      }
      close() {}
      send() {}
    }
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const client = new WsClient("wss://app.example/api/v1/browser/ws");
    client.connect();
    FakeWebSocket.instances[0].onclose?.({ code: 1006 } as CloseEvent);
    client.disconnect();
    vi.runAllTimers();

    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});


describe("transport health", () => {
  it("reconnects an unresponsive socket and ignores stale close events", () => {
    vi.useFakeTimers();
    class FakeWebSocket {
      static OPEN = 1; static CONNECTING = 0;
      static instances: FakeWebSocket[] = [];
      readyState = 0;
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      send = vi.fn();
      close = vi.fn((code = 1000) => { this.readyState = 3; this.onclose?.({ code } as CloseEvent); });
      constructor() { FakeWebSocket.instances.push(this); }
    }
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const client = new WsClient("wss://app.example/ws");
    const closed = vi.fn(); client.onClose(closed); client.connect();
    expect(client.isConnected()).toBe(false);
    const first = FakeWebSocket.instances[0]; first.readyState = 1; first.onopen!();
    expect(client.isConnected()).toBe(true);
    vi.advanceTimersByTime(30_000); expect(first.close).not.toHaveBeenCalled();
    vi.advanceTimersByTime(15_000); expect(first.close).toHaveBeenCalledWith(4000, "Heartbeat timeout");
    expect(client.isConnected()).toBe(false);
    vi.advanceTimersByTime(1000); expect(FakeWebSocket.instances).toHaveLength(2);
    const second = FakeWebSocket.instances[1]; second.readyState = 1; second.onopen!();
    first.onclose!({ code: 4401 } as CloseEvent);
    expect(closed).toHaveBeenCalledTimes(1); expect(client.isConnected()).toBe(true);
    vi.advanceTimersByTime(30_000);
    second.onmessage!({ data: JSON.stringify({ v: 1, kind: "echo", id: "ping", channel: "system", transport: "t", data: {} }) } as MessageEvent);
    vi.advanceTimersByTime(30_000); expect(second.close).not.toHaveBeenCalled();
    client.disconnect(); expect(client.isConnected()).toBe(false);
  });
});
