import { describe, expect, it } from "vitest";
import { redactNetworkEvent } from "./network-redaction";

describe("CDP network credential redaction", () => {
  it("redacts cookie objects and headers without mutating Chrome's event", () => {
    const event = {
      requestId: "r1", headers: { Cookie: "sid=secret", Authorization: "Bearer secret", Accept: "image/png" },
      associatedCookies: [{ cookie: { name: "sid", value: "secret", domain: "example.com", httpOnly: true }, blockedReasons: [] }],
      headersText: "HTTP/1.1 200 OK\r\nSet-Cookie: sid=secret",
    };
    const output = redactNetworkEvent("Network.requestWillBeSentExtraInfo", event);
    expect(JSON.stringify(output)).not.toContain("secret");
    expect(output.associatedCookies).toEqual([{ cookie: { name: "sid", value: "[redacted]", domain: "example.com", httpOnly: true }, blockedReasons: [] }]);
    expect(output.headers).toEqual({ Cookie: "[redacted]", Authorization: "[redacted]", Accept: "image/png" });
    expect(event.headers.Cookie).toBe("sid=secret");
  });

  it("preserves download routing fields and content headers in Fetch pauses", () => {
    const output = redactNetworkEvent("Fetch.requestPaused", {
      requestId: "f1", networkId: "r1", resourceType: "Document", responseStatusCode: 200,
      request: { url: "https://example.com/report", method: "POST", headers: { cookie: "secret" } },
      responseHeaders: [{ name: "set-cookie", value: "secret" }, { name: "Content-Disposition", value: "attachment; filename=report.csv" }],
    });
    expect(JSON.stringify(output)).not.toContain("secret");
    expect(output).toMatchObject({ requestId: "f1", networkId: "r1", responseStatusCode: 200,
      request: { url: "https://example.com/report", method: "POST" },
      responseHeaders: [{ name: "set-cookie", value: "[redacted]" }, { name: "Content-Disposition", value: "attachment; filename=report.csv" }],
    });
  });

  it("redacts blocked cookie lines but does not change explicit scoped export or page results", () => {
    expect(redactNetworkEvent("Network.responseReceivedExtraInfo", {
      blockedCookies: [{ cookieLine: "sid=secret", blockedReasons: ["SecureOnly"] }],
    })).toEqual({ blockedCookies: [{ cookieLine: "[redacted]", blockedReasons: ["SecureOnly"] }] });
    const allowed = { cookies: [{ name: "sid", value: "explicit-grant" }] };
    expect(redactNetworkEvent("Flowork.cookieExport", allowed)).toBe(allowed);
    expect(redactNetworkEvent("Runtime.bindingCalled", { payload: "file-bytes" })).toEqual({ payload: "file-bytes" });
  });
});
