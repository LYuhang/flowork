/** Keep passive CDP diagnostics from becoming an implicit Cookie export. */
const SECRET_HEADER = /^(authorization|proxy-authorization|cookie|set-cookie|x-api-key)$/i;

export function redactNetworkEvent(method: string, params: Record<string, unknown>): Record<string, unknown> {
  if (!method.startsWith("Network.") && !method.startsWith("Fetch.")) return params;
  const visit = (value: unknown, field = ""): unknown => {
    if (field === "cookieLine" || field === "headersText" || field === "requestHeadersText") return "[redacted]";
    if (Array.isArray(value)) {
      if (/headers$/i.test(field)) return value.map(header => {
        if (header && typeof header === "object" && "name" in header && SECRET_HEADER.test(String(header.name)))
          return { ...header, value: "[redacted]" };
        return visit(header);
      });
      return value.map(item => visit(item, field === "cookies" ? "cookie" : ""));
    }
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.entries(value).map(([key, item]) => [key,
        (/headers$/i.test(field) && SECRET_HEADER.test(key)) || (field === "cookie" && key === "value")
          ? "[redacted]" : visit(item, key),
      ]));
    }
    return value;
  };
  return visit(params) as Record<string, unknown>;
}
