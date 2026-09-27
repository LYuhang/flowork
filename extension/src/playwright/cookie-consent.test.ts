import { describe, expect, it, vi } from "vitest";
import { CookieConsentStore, accessCookies, cookieExport, type CookieConsentScope } from "./cookie-consent";

const scope: CookieConsentScope = { sessionId: "session", generation: 1, chatId: "chat", windowId: 7 };
const tab = { id: 10, windowId: 7, url: "https://example.test/account" };
const secret = { name: "session", value: "NEVER_PRINT_THIS", domain: "example.test", path: "/", httpOnly: true, secure: true, expires: -1 };

function fixture() {
  const data: Record<string, unknown> = {};
  const storage = { get: vi.fn(async () => structuredClone(data)), set: vi.fn(async items => { Object.assign(data, structuredClone(items)); }) };
  const changed = vi.fn();
  const store = new CookieConsentStore(storage, changed);
  return { store, storage, changed };
}

describe("site-scoped Cookie export consent", () => {
  it("metadata does not expose values or request export approval", async () => {
    const { store } = fixture();
    const output = await accessCookies(store, scope, tab, "metadata", "json", async () => [secret]);
    expect(JSON.stringify(output)).not.toContain(secret.value);
    expect(output.cookies).toEqual([{ name: "session", domain: "example.test", path: "/", httpOnly: true, secure: true, expires: -1 }]);
    expect(await store.list(scope)).toEqual([]);
  });

  it("denies export before reading credentials, then exports only after a separate decision", async () => {
    const { store } = fixture();
    const read = vi.fn(async () => [secret]);
    const denied = await accessCookies(store, scope, tab, "export", "json", read);
    expect(denied.error).toBe("cookie_consent_required");
    expect(read).not.toHaveBeenCalled();
    const [request] = await store.list(scope);
    await store.decide(scope, request.id, true);
    const output = await accessCookies(store, scope, tab, "export", "json", read);
    expect(JSON.parse(String(output.content)).cookies).toEqual([secret]);
    expect(output.grant_id).toBe(`${request.id}:2`);
    expect(read).toHaveBeenCalledWith(10, tab.url);
    const status = await accessCookies(store, scope, tab, "status", "json", read);
    expect(status.allowed).toBe(true);
    expect(read).toHaveBeenCalledTimes(1);
  });

  it("cannot reuse consent across origins, chats, windows or lease generations", async () => {
    const { store } = fixture();
    const request = await store.request(scope, tab.url);
    await store.decide(scope, request.id, true);
    for (const changed of [{ ...scope, generation: 2 }, { ...scope, chatId: "another" }, { ...scope, windowId: 8 }]) {
      expect(await store.lookup(changed, tab.url)).toBeUndefined();
      await expect(store.decide(changed, request.id, true)).rejects.toThrow("expired");
    }
    expect(await store.lookup(scope, "https://other.test/account")).toBeUndefined();
  });

  it("revocation during a read prevents the credential response and notifies cleanup", async () => {
    const { store, changed } = fixture();
    const request = await store.request(scope, tab.url);
    await store.decide(scope, request.id, true);
    const result = await accessCookies(store, scope, tab, "export", "json", async () => {
      await store.decide(scope, request.id, false);
      return [secret];
    });
    expect(result.error).toBe("cookie_consent_revoked");
    expect(JSON.stringify(result)).not.toContain(secret.value);
    expect(changed).toHaveBeenCalledWith(`${request.id}:2`);
  });

  it("survives worker restart within a lease, but clears on release", async () => {
    const { store, storage } = fixture();
    const request = await store.request(scope, tab.url);
    await store.decide(scope, request.id, true);
    const restarted = new CookieConsentStore(storage);
    expect((await restarted.lookup(scope, tab.url))?.state).toBe("allowed");
    await restarted.retain(null);
    expect(await restarted.list(scope)).toEqual([]);
  });

  it("does not silently lose partition or Netscape field semantics", async () => {
    expect(cookieExport([{ ...secret, partitionKey: { topLevelSite: "https://example.test" } }], tab.url, "netscape").error).toBe("cookie_format_lossy");
    expect(cookieExport([{ ...secret, value: "a\tb" }], tab.url, "netscape").error).toBe("cookie_format_lossy");
    expect(cookieExport([secret], tab.url, "netscape").content).toContain("#HttpOnly_example.test\tFALSE\t/\tTRUE\t0\tsession\t");
    const { store } = fixture();
    const output = await accessCookies(store, scope, tab, "metadata", "json", async () => [
      secret, { ...secret, name: "unrelated", partitionKey: { topLevelSite: "https://other.test" } },
      { ...secret, name: "opaque", partitionKeyOpaque: true },
    ]);
    expect(output.cookies).toHaveLength(1);
  });

  it("never stores Cookie values alongside consent records", async () => {
    const { store, storage } = fixture();
    await store.request(scope, tab.url);
    expect(JSON.stringify(storage.set.mock.calls)).not.toContain(secret.value);
    expect(JSON.stringify(storage.set.mock.calls)).not.toContain("/account");
  });
});
