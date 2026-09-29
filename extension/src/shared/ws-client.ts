/**
 * Offscreen-owned WebSocket client (B1: the socket lives in the offscreen
 * document so it survives service-worker eviction). Holds one connection to the
 * backend hub, replies to nothing on its own (the extension stays thin, §4.1) —
 * it just surfaces opens/echoes and reconnects with capped exponential backoff.
 *
 * On reconnect it loses no app state the backend can't re-drive: the host
 * registry re-keys the transport on the new socket. Ping, lifecycle, and
 * Playwright relay frames share the same transport.
 */
import { encode, decode, type Envelope } from "./envelope";

/**
 * Capped exponential backoff: 1s, 2s, 4s, 8s, 16s, then pinned at 30s.
 * Pure function so the curve is unit-testable without a live socket.
 */
export function backoffMs(attempt: number): number {
  return Math.min(1000 * 2 ** attempt, 30000);
}

let _seq = 0;
/** Process-unique correlation id for a ping (the id an echo will carry back). */
function corr(): string {
  return `c${Date.now()}_${_seq++}`;
}

export class WsClient {
  private static readonly MAX_PENDING_FRAMES = 256;
  private ws: WebSocket | null = null;
  private attempt = 0;
  private closed = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pendingFrames: string[] = [];
  private lastReceivedAt = 0;
  private heartbeat: ReturnType<typeof setInterval> | null = null;
  private openCbs: (() => void)[] = [];
  private closeCbs: ((event: CloseEvent) => void)[] = [];
  private authRequiredCbs: (() => void)[] = [];
  private echoCbs: ((e: Envelope) => void)[] = [];
  private playwrightRelayCbs: ((e: Envelope) => void)[] = [];
  private refreshes = new Map<string, (ok: boolean) => void>();
  private authRenewTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly url: string,
    private protocols: readonly string[] = [],
  ) {}

  onOpen(cb: () => void): void {
    this.openCbs.push(cb);
  }

  onClose(cb: (event: CloseEvent) => void): void {
    this.closeCbs.push(cb);
  }

  onAuthRequired(cb: () => void): void {
    this.authRequiredCbs.push(cb);
  }

  onEcho(cb: (e: Envelope) => void): void {
    this.echoCbs.push(cb);
  }

  /** Subscribe to the authenticated Playwright extension relay data plane. */
  onPlaywrightRelay(cb: (e: Envelope) => void): void {
    this.playwrightRelayCbs.push(cb);
  }

  connect(): void {
    if (
      this.ws &&
      (this.ws.readyState === WebSocket.CONNECTING ||
        this.ws.readyState === WebSocket.OPEN)
    ) {
      return;
    }
    this.closed = false;
    const ws = new WebSocket(this.url, [...this.protocols]);
    this.ws = ws;

    ws.onopen = () => {
      if (this.ws !== ws || this.closed) return;
      this.lastReceivedAt = Date.now();
      this.attempt = 0; // reset backoff once we have a live socket
      this.stopHeartbeat();
      // The Runtime may spend tens of seconds reasoning before its first
      // browser call. Keep the transport active through reverse proxies and
      // jump-host tunnels during that idle period; the backend already answers
      // protocol pings with an echo.
      this.heartbeat = setInterval(() => {
        if (Date.now() - this.lastReceivedAt >= 45_000) {
          this.stopHeartbeat();
          ws.close(4000, "Heartbeat timeout");
          return;
        }
        this.ping({ type: "keepalive" });
      }, 15_000);
      const pending = this.pendingFrames.splice(0);
      for (const raw of pending) ws.send(raw);
      for (const cb of this.openCbs) cb();
    };

    ws.onmessage = (ev: MessageEvent) => {
      if (this.ws !== ws || this.closed) return;
      let e: Envelope;
      try {
        e = decode(String(ev.data));
      } catch {
        return; // ignore malformed frames; never eval server payloads (§6)
      }
      this.lastReceivedAt = Date.now();
      if (e.kind === "echo") {
        const result = e.data as { type?: string; ok?: boolean; expires_at?: number } | null;
        if ((result?.type === "auth_status" || (result?.type === "auth_refresh" && result.ok === true))
            && typeof result.expires_at === "number" && Number.isFinite(result.expires_at)) {
          if (this.authRenewTimer) clearTimeout(this.authRenewTimer);
          // The server supplies this deadline after authenticating the token.
          // Ask the authenticated iframe for a replacement before expiry, while
          // the existing socket and pending commands remain alive.
          this.authRenewTimer = setTimeout(() => {
            this.authRenewTimer = null;
            if (this.ws === ws && !this.closed) for (const cb of this.authRequiredCbs) cb();
          }, Math.max(1000, Math.min(2 ** 31 - 1, result.expires_at * 1000 - Date.now() - 60000)));
        }
        if (result?.type === "auth_refresh") this.refreshes.get(e.id)?.(result.ok === true);
        for (const cb of this.echoCbs) cb(e);
      } else if (e.kind === "playwright_relay") {
        for (const cb of this.playwrightRelayCbs) cb(e);
      }
    };

    ws.onclose = (event: CloseEvent) => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.stopHeartbeat();
      for (const finish of this.refreshes.values()) finish(false);
      if (this.closed) return; // intentional close: do not reconnect
      for (const cb of this.closeCbs) cb(event);
      if (event.code === 4401) {
        // The scoped capability expired or was revoked. Reusing it in a
        // reconnect loop can never succeed; ask the authenticated embed to mint
        // a fresh browser-bound capability without killing the browser session.
        this.closed = true;
        for (const cb of this.authRequiredCbs) cb();
        return;
      }
      const delay = backoffMs(this.attempt++);
      this.clearReconnectTimer();
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null;
        if (!this.closed) this.connect();
      }, delay); // reconnect; host re-drives state
    };
  }

  /** Whether this client still owns a live, connecting, or backoff transport. */
  isActive(): boolean {
    return !this.closed;
  }

  isConnected(): boolean {
    return !this.closed && this.ws?.readyState === WebSocket.OPEN;
  }

  /** Only a signed, same-identity token accepted by the server may renew this
   * socket. Never infer authentication from decoding a token in the browser. */
  async refreshAuthentication(token: string, protocols: readonly string[]): Promise<boolean> {
    const socket = this.ws;
    if (!socket || socket.readyState !== WebSocket.OPEN || this.closed) return false;
    const id = corr();
    return new Promise(resolve => {
      const timer = setTimeout(() => finish(false), 5000);
      const finish = (ok: boolean) => {
        if (!this.refreshes.delete(id)) return;
        clearTimeout(timer);
        const accepted = ok && this.ws === socket && !this.closed;
        if (accepted) this.protocols = protocols;
        resolve(accepted);
      };
      this.refreshes.set(id, finish);
      try { socket.send(encode("auth_refresh", { id, channel: "system", transport: "pending", data: { token } })); }
      catch { finish(false); }
    });
  }

  /** Stop reconnecting and drop the socket. */
  disconnect(): void {
    this.closed = true;
    this.stopHeartbeat();
    this.clearReconnectTimer();
    this.pendingFrames = [];
    for (const finish of this.refreshes.values()) finish(false);
    const ws = this.ws;
    this.ws = null;
    ws?.close();
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private stopHeartbeat(): void {
    if (this.authRenewTimer) clearTimeout(this.authRenewTimer);
    this.authRenewTimer = null;
    if (this.heartbeat !== null) {
      clearInterval(this.heartbeat);
      this.heartbeat = null;
    }
  }

  /**
   * Send a `ping` and return the correlation id the matching `echo` will carry.
   * `transport` is "pending" because the host stamps the authoritative
   * `transport_id` (`<tenant>:<browser>`) onto the echo it returns.
   */
  ping(data: unknown): string | null {
    if (this.ws?.readyState !== WebSocket.OPEN) return null;
    const id = corr();
    this.ws.send(
      encode("ping", { id, channel: "system", transport: "pending", data }),
    );
    return id;
  }

  /**
   * Send a pre-encoded lifecycle or Playwright relay frame verbatim. The
   * extension never constructs Agent run state, so this remains a thin
   * passthrough to the socket.
   */
  sendRaw(raw: string): boolean {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(raw);
      return true;
    }
    if (this.closed) return false;
    // Playwright can answer an initialization request while the replacement
    // socket is still handshaking. Preserve that response instead of throwing
    // InvalidStateError and forcing the server-side MCP to wait for its timeout.
    if (this.pendingFrames.length >= WsClient.MAX_PENDING_FRAMES) {
      this.pendingFrames.shift();
    }
    this.pendingFrames.push(raw);
    return true;
  }
}
