/** Credential export consent is local to the trusted extension, never granted
 * by page JavaScript, a CDP command, an Agent assertion or iframe postMessage. */
export type CookieConsentScope = {
  sessionId: string;
  generation: number;
  chatId: string;
  windowId: number;
};

export type CookieConsent = CookieConsentScope & {
  id: string;
  origin: string;
  state: "requested" | "allowed" | "denied";
  revision: number;
};

export type CookieConsentStorage = {
  get(key: string): Promise<Record<string, unknown>>;
  set(items: Record<string, unknown>): Promise<void>;
};

const STORAGE_KEY = "browserCookieExportConsents";

function sameScope(left: CookieConsentScope, right: CookieConsentScope): boolean {
  return left.sessionId === right.sessionId && left.generation === right.generation
    && left.chatId === right.chatId && left.windowId === right.windowId;
}

export function cookieOrigin(url: string): string {
  const parsed = new URL(url);
  if (!["http:", "https:"].includes(parsed.protocol)) throw new Error("Cookie access requires an HTTP(S) page");
  return parsed.origin;
}

export class CookieConsentStore {
  private records: CookieConsent[] = [];
  private readonly ready: Promise<void>;
  private writing = Promise.resolve();

  constructor(private readonly storage: CookieConsentStorage,
    private readonly changed: (revokedGrant?: string) => void = () => undefined) {
    this.ready = storage.get(STORAGE_KEY).then(value => {
      const records = value[STORAGE_KEY];
      if (Array.isArray(records)) this.records = records.filter(item => item
        && typeof item.id === "string" && typeof item.origin === "string"
        && typeof item.sessionId === "string" && typeof item.chatId === "string"
        && Number.isInteger(item.generation) && Number.isInteger(item.windowId)
        && Number.isInteger(item.revision) && ["requested", "allowed", "denied"].includes(item.state));
    });
  }

  private async save(revokedGrant?: string): Promise<void> {
    const snapshot = this.records.map(record => ({ ...record }));
    this.writing = this.writing.catch(() => {}).then(() => this.storage.set({ [STORAGE_KEY]: snapshot }));
    await this.writing;
    this.changed(revokedGrant);
  }

  async lookup(scope: CookieConsentScope, url: string): Promise<CookieConsent | undefined> {
    await this.ready;
    const origin = cookieOrigin(url);
    const record = this.records.find(item => sameScope(item, scope) && item.origin === origin);
    return record ? { ...record } : undefined;
  }

  async request(scope: CookieConsentScope, url: string): Promise<CookieConsent> {
    const existing = await this.lookup(scope, url);
    if (existing) return existing;
    const concurrent = this.records.find(item => sameScope(item, scope) && item.origin === cookieOrigin(url));
    if (concurrent) return { ...concurrent };
    const record: CookieConsent = { ...scope, id: crypto.randomUUID(), origin: cookieOrigin(url), state: "requested", revision: 1 };
    this.records.push(record);
    await this.save();
    return { ...record };
  }

  async list(scope: CookieConsentScope): Promise<CookieConsent[]> {
    await this.ready;
    return this.records.filter(item => sameScope(item, scope)).map(item => ({ ...item }));
  }

  async decide(scope: CookieConsentScope, id: string, allow: boolean): Promise<void> {
    await this.ready;
    const record = this.records.find(item => sameScope(item, scope) && item.id === id);
    if (!record) throw new Error("This Cookie export request belongs to an expired browser-control session");
    const previousGrant = record.state === "allowed" ? `${record.id}:${record.revision}` : undefined;
    record.state = allow ? "allowed" : "denied";
    record.revision++;
    // Revocation takes effect in memory before storage I/O or notification.
    try { await this.save(previousGrant); }
    catch (error) {
      record.state = "denied";
      record.revision++;
      this.changed(previousGrant);
      throw error;
    }
  }

  async retain(scope: CookieConsentScope | null): Promise<void> {
    await this.ready;
    const removed = this.records.filter(item => !scope || !sameScope(item, scope));
    this.records = this.records.filter(item => scope && sameScope(item, scope));
    if (!removed.length) return;
    await this.save();
    for (const record of removed) if (record.state === "allowed") this.changed(`${record.id}:${record.revision}`);
  }
}

export type Cookie = Record<string, unknown> & { name: string; value: string; domain: string; path: string };

export function cookieExport(cookies: Cookie[], url: string, format: string): { content?: string; error?: string; message?: string; hint?: string } {
  if (format === "json") return { content: JSON.stringify({ url, cookies }, null, 2) + "\n" };
  if (format !== "netscape") return { error: "invalid_cookie_format", message: "Use json or netscape." };
  if (cookies.some(cookie => cookie.partitionKey || cookie.partitionKeyOpaque))
    return { error: "cookie_format_lossy", message: "Netscape format cannot preserve partitioned Cookies.", hint: "Use --format json to retain partition attributes." };
  if (cookies.some(cookie => [cookie.name, cookie.value, cookie.domain, cookie.path].some(value => /[\t\r\n]/.test(value))))
    return { error: "cookie_format_lossy", message: "A Cookie contains characters Netscape format cannot represent.", hint: "Use --format json." };
  return { content: "# Netscape HTTP Cookie File\n" + cookies.map(cookie => [
    `${cookie.httpOnly ? "#HttpOnly_" : ""}${cookie.domain}`,
    cookie.domain.startsWith(".") ? "TRUE" : "FALSE", cookie.path,
    cookie.secure ? "TRUE" : "FALSE", Number(cookie.expires) > 0 ? Math.floor(Number(cookie.expires)) : 0,
    cookie.name, cookie.value,
  ].join("\t")).join("\n") + "\n" };
}

export async function accessCookies(
  store: CookieConsentStore, scope: CookieConsentScope,
  tab: { id: number; windowId: number; url: string }, mode: "metadata" | "export" | "status", format: string,
  read: (tabId: number, url: string) => Promise<Cookie[]>,
): Promise<Record<string, unknown>> {
  if (tab.windowId !== scope.windowId) throw new Error("Cookie target is outside the authorized side-panel window");
  cookieOrigin(tab.url);
  const consent = mode === "export" ? await store.request(scope, tab.url) : await store.lookup(scope, tab.url);
  const grant = consent?.state === "allowed" ? `${consent.id}:${consent.revision}` : null;
  if (mode === "status") return { allowed: !!grant, grant_id: grant, origin: cookieOrigin(tab.url) };
  if (mode === "export" && !grant) return {
    error: consent?.state === "denied" ? "cookie_consent_denied" : "cookie_consent_required",
    message: "Cookie export needs separate user permission in the extension side panel.",
    hint: "Ask the user to review Cookie export permissions for this origin in the side panel. Do not retry until they allow it; normal page actions need no Cookie export.",
    origin: cookieOrigin(tab.url),
  };
  const all = await read(tab.id, tab.url);
  // The URL-filtered CDP call keeps domain/path/secure matching in Chrome.
  // Exclude cookies partitioned for another top-level site and opaque contexts.
  const hostname = new URL(tab.url).hostname;
  const cookies = all.filter(cookie => {
    if (cookie.partitionKeyOpaque) return false;
    const key = cookie.partitionKey as { topLevelSite?: string; hasCrossSiteAncestor?: boolean } | undefined;
    if (!key) return true;
    if (!key.topLevelSite || key.hasCrossSiteAncestor) return false;
    try {
      const site = new URL(key.topLevelSite);
      return site.protocol === new URL(tab.url).protocol
        && (hostname === site.hostname || hostname.endsWith(`.${site.hostname}`));
    } catch { return false; }
  });
  if (mode === "metadata") return { url: tab.url, cookies: cookies.map(({ value: _value, ...metadata }) => metadata), export_allowed: !!grant };
  // Revocation while Chrome is reading must not complete the export.
  const latest = await store.lookup(scope, tab.url);
  if (latest?.state !== "allowed" || `${latest.id}:${latest.revision}` !== grant)
    return { error: "cookie_consent_revoked", message: "Cookie export permission changed; no credentials were exported." };
  const exported = cookieExport(cookies, tab.url, format);
  return exported.error ? exported : { ...exported, cookie_count: cookies.length, url: tab.url, grant_id: grant };
}
