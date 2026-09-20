/**
 * Bundle-local extension config.
 *
 * WEB_BASE is the origin of the Flowork web app the side panel embeds via an
 * <iframe> (`<WEB_BASE>/embed/chat?...`). It is NOT a secret — it is a public
 * origin baked into the extension bundle.
 *
 * CRITICAL: this MUST be the SAME origin the user actually loads the web app
 * from — i.e. the same origin the web app derives `wsBase` from
 * (`location.origin`, see web/src/lib/extension.ts). If they differ, the embed
 * iframe loads from one origin while the browser WS targets another → the side
 * panel shows "localhost refused" / "domains, protocols and ports must match".
 *
 * Set it per environment at BUILD time via `VITE_WEB_BASE`:
 *   - local (port-forwarded localhost):  http://localhost:9001   (http — the
 *     dev server is plain HTTP, NOT https)
 *   - IP-only / WSL:                      http://192.168.1.20:9001
 *   - IPv6:                               http://[2001:db8::20]:9001
 *   - dev box via workspace-proxy:        https://<your-proxy-host>   (the same
 *     origin you use to open the app)
 *   - production:                         https://app.example.com
 *
 * The default is local-HTTP dev. `VITE_EXTENSION_ALLOWED_ORIGINS` is the
 * compile-time source of truth for both the generated manifest allowlist and
 * the runtime sender check. Entries are comma-separated web bases and may
 * include a reverse-proxy path prefix. IP literals and explicit ports are
 * supported; every entry must still be an exact HTTP(S) base.
 */
export const WEB_BASE =
  (import.meta.env?.VITE_WEB_BASE as string | undefined) ?? "http://localhost:9001";

/**
 * The browser-WS base (ws(s)://host[/prefix]). A COLD side-panel open (entry B,
 * the user clicks the toolbar icon with no main-app handoff) has no binding, so
 * the embed's OPEN_WS carries no wsBase and the SW has none stored — without a
 * baked fallback the offscreen gets an empty wsBase and bails. Default: derive
 * from WEB_BASE (same host+prefix, http→ws); override at BUILD time via
 * VITE_WS_BASE when the WS endpoint differs from the web origin.
 */
export const WS_BASE =
  (import.meta.env?.VITE_WS_BASE as string | undefined) ??
  WEB_BASE.replace(/^http/, "ws");

function normalizeWebBase(value: string): string | null {
  try {
    const url = new URL(value);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    if (url.username || url.password || url.search || url.hash) return null;
    const path = url.pathname.replace(/\/+$/, "");
    return `${url.origin}${path}`;
  } catch {
    return null;
  }
}

const configuredAllowedOrigins =
  (import.meta.env?.VITE_EXTENSION_ALLOWED_ORIGINS as string | undefined) ?? WEB_BASE;

export const ALLOWED_WEB_BASES = [
  ...new Set(
    configuredAllowedOrigins
      .split(",")
      .map((value) => normalizeWebBase(value.trim()))
      .filter((value): value is string => !!value),
  ),
];

/** Return the canonical configured base only for an exact allowlisted binding. */
export function resolveAllowedWebBase(value: string | undefined): string | null {
  const normalized = normalizeWebBase(value || WEB_BASE);
  return normalized && ALLOWED_WEB_BASES.includes(normalized) ? normalized : null;
}

/**
 * Runtime defense for externally_connectable messages. The manifest may need
 * wildcard subdomains for several deployment environments, but only the exact
 * origin and reverse-proxy path baked into this build is a trusted web-app
 * sender. This prevents another application on an allowed corporate domain
 * from clearing or replacing extension handoff state.
 */
export function isAllowedWebAppSenderUrl(value: string | undefined): boolean {
  if (!value) return false;
  try {
    const sender = new URL(value);
    return ALLOWED_WEB_BASES.some((base) => {
      const configured = new URL(base);
      if (sender.origin !== configured.origin) return false;
      const prefix = configured.pathname.replace(/\/+$/, "");
      return !prefix || sender.pathname === prefix || sender.pathname.startsWith(`${prefix}/`);
    });
  } catch {
    return false;
  }
}
