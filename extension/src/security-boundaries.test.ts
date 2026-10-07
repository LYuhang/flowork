import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

interface Manifest {
  manifest_version: number;
  permissions?: string[];
  host_permissions?: string[];
  content_scripts?: Array<Record<string, unknown>>;
  externally_connectable?: { matches?: string[] };
  web_accessible_resources?: unknown[];
  key?: string;
}

const extensionRoot = fileURLToPath(new URL("..", import.meta.url));
const webRoot = fileURLToPath(new URL("../../web/", import.meta.url));
const manifest = JSON.parse(
  readFileSync(new URL("manifest.json", `file://${extensionRoot}/`), "utf8"),
) as Manifest;

describe("MV3 extension security boundaries", () => {
  it("keeps the manifest key, API identity, and Web framing identity aligned", () => {
    expect(manifest.key).toBeTruthy();
    const digest = createHash("sha256")
      .update(Buffer.from(manifest.key!, "base64"))
      .digest()
      .subarray(0, 16);
    const extensionId = [...digest]
      .flatMap((byte) => [byte >> 4, byte & 0x0f])
      .map((nibble) => String.fromCharCode("a".charCodeAt(0) + nibble))
      .join("");
    expect(extensionId).toBe("mkfldhmlgdbpmhplaphhcfcdcoaakcik");
  });

  it("keeps broad page access only where the visible control island needs it", () => {
    expect(manifest.manifest_version).toBe(3);
    expect(manifest.host_permissions ?? []).not.toContain("<all_urls>");
    expect(manifest.content_scripts).toEqual([
      expect.objectContaining({
        matches: ["<all_urls>"],
        js: ["island/content.js"],
        all_frames: false,
        world: "ISOLATED",
      }),
    ]);
    expect(manifest.web_accessible_resources ?? []).toEqual([]);
    expect(manifest.host_permissions ?? []).not.toContain("<all_urls>");
  });

  it("retains required automation APIs without accepting plaintext remote peers", () => {
    expect(new Set(manifest.permissions)).toEqual(
      new Set(["contextMenus", "offscreen", "sidePanel", "storage", "debugger", "tabs", "scripting", "downloads", "webRequest", "clipboardWrite"]),
    );
    expect(manifest.host_permissions).toEqual(["http://*/*", "https://*/*", "file:///*"]);
    expect(manifest.permissions).not.toContain("webRequestBlocking");
    const matches = manifest.externally_connectable?.matches ?? [];
    // Source manifest is a non-loadable template. Vite injects the exact
    // VITE_EXTENSION_ALLOWED_ORIGINS set into dist/manifest.json at build time.
    expect(matches).toEqual([]);
    const buildConfig = readFileSync(
      new URL("vite.config.ts", `file://${extensionRoot}/`),
      "utf8",
    );
    expect(buildConfig).toContain("VITE_EXTENSION_ALLOWED_ORIGINS");
    expect(buildConfig).toContain("manifest.externally_connectable");
    expect(buildConfig).toContain("manifest.host_permissions");
    expect(buildConfig).not.toContain("*.service.example.test");
  });

  it("never reconstructs the browser credential as a URL query", () => {
    const offscreen = readFileSync(
      new URL("src/offscreen.ts", `file://${extensionRoot}/`),
      "utf8",
    );
    expect(offscreen).not.toMatch(/browser\/ws[^\n]*(?:token|scopedToken)=/);
    expect(offscreen).toContain("browserWsProtocols");
  });

  it("pins production embed framing to one validated extension id", () => {
    const nginx = readFileSync(
      new URL("nginx.conf", `file://${webRoot}/`),
      "utf8",
    );
    const dockerfile = readFileSync(
      new URL("Dockerfile", `file://${webRoot}/`),
      "utf8",
    );
    const validator = readFileSync(
      new URL("15-validate-extension-id.sh", `file://${webRoot}/`),
      "utf8",
    );
    expect(nginx).toContain("chrome-extension://${VIBECANVAS_BROWSER_EXTENSION_ID}");
    expect(nginx).not.toContain("chrome-extension://*");
    expect(dockerfile).toContain(
      "NGINX_ENVSUBST_FILTER=^(VIBECANVAS_BROWSER_EXTENSION_ID|CSP_SCRIPT_HASHES)$",
    );
    expect(dockerfile).toContain("/docker-entrypoint.d/14-load-csp-hashes.envsh");
    expect(dockerfile).toContain("/docker-entrypoint.d/15-validate-extension-id.sh");
    expect(validator).toContain('${#extension_id}');
    expect(validator).toContain('*[!a-p]*');
  });
});
