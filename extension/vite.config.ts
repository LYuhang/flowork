import { resolve } from "node:path";
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { defineConfig, loadEnv, type Plugin } from "vite";

const root = resolve(__dirname);

/**
 * Copy the static MV3 assets (manifest + HTML host pages) into dist/ after the
 * bundle is written. The HTML pages are deliberately NOT processed as Vite HTML
 * inputs: they are tiny static shells that load the emitted ESM entry by a
 * fixed name (offscreen.js / sidepanel.js), matching what the manifest expects.
 */
function normalizeAllowedWebBases(raw: string, webBase: string): string[] {
  const configured = (raw || webBase)
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  if (configured.length === 0) {
    throw new Error("VITE_EXTENSION_ALLOWED_ORIGINS must contain at least one web base");
  }

  const bases = configured.map((value) => {
    const url = new URL(value);
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      throw new Error(`Unsupported extension web origin protocol: ${url.protocol}`);
    }
    // Exact HTTP origins are intentionally supported for IP-only/WSL/local
    // deployments. This is a compile-time allowlist, never a wildcard; the
    // deployment guide still recommends HTTPS whenever traffic leaves the
    // trusted machine/network.
    if (url.username || url.password || url.search || url.hash) {
      throw new Error(`Extension web origins cannot contain credentials, query, or hash: ${value}`);
    }
    const path = url.pathname.replace(/\/+$/, "");
    return `${url.origin}${path}`;
  });

  const normalizedWebBase = (() => {
    const url = new URL(webBase);
    return `${url.origin}${url.pathname.replace(/\/+$/, "")}`;
  })();
  if (!bases.includes(normalizedWebBase)) {
    throw new Error("VITE_WEB_BASE must also be listed in VITE_EXTENSION_ALLOWED_ORIGINS");
  }
  return [...new Set(bases)];
}

function chromeOriginMatch(base: string): string {
  const url = new URL(base);
  return `${url.protocol}//${url.host}/*`;
}

function copyStaticAssets(allowedWebBases: string[]): Plugin {
  return {
    name: "copy-mv3-static-assets",
    apply: "build",
    closeBundle() {
      const out = resolve(root, "dist");
      for (const f of ["offscreen.html", "sidepanel.html"]) {
        copyFileSync(resolve(root, f), resolve(out, f));
      }
      const iconsOut = resolve(out, "icons");
      mkdirSync(iconsOut, { recursive: true });
      for (const icon of ["icon-light.png", "icon-dark.png"]) {
        copyFileSync(
          resolve(root, "../web/public/branding", icon),
          resolve(iconsOut, icon),
        );
      }
      // Chrome expects a neutral manifest icon. Reuse the light brand asset
      // instead of keeping an otherwise duplicate icon.png in the repository.
      copyFileSync(
        resolve(root, "../web/public/branding/icon-light.png"),
        resolve(iconsOut, "icon.png"),
      );
      const manifest = JSON.parse(
        readFileSync(resolve(root, "manifest.json"), "utf8"),
      ) as {
        externally_connectable?: { matches?: string[] };
        host_permissions?: string[];
      };
      const originMatches = [...new Set(allowedWebBases.map(chromeOriginMatch))];
      manifest.externally_connectable = {
        matches: originMatches,
      };
      // Download provenance observes HTTP(S) requests; approved native file
      // reads require file:// plus Chrome's separate user-controlled toggle.
      // These host grants do NOT expand externally_connectable or the app
      // recovery path's own exact-origin allowlist.
      manifest.host_permissions = [...new Set([...(manifest.host_permissions ?? []), ...originMatches])];
      writeFileSync(
        resolve(out, "manifest.json"),
        `${JSON.stringify(manifest, null, 2)}\n`,
        "utf8",
      );
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, root, "VITE_");
  const webBase = env.VITE_WEB_BASE || "http://localhost:9001";
  const allowedWebBases = normalizeAllowedWebBases(
    env.VITE_EXTENSION_ALLOWED_ORIGINS || "",
    webBase,
  );

  return {
    build: {
      outDir: "dist",
      emptyOutDir: true,
      target: "es2022",
      minify: false,
      rollupOptions: {
        input: {
          "service-worker": resolve(root, "src/service-worker.ts"),
          offscreen: resolve(root, "src/offscreen.ts"),
          sidepanel: resolve(root, "src/sidepanel.ts"),
          // Dynamic Island content script. The input KEY is "island/content" so
          // entryFileNames:"[name].js" emits it at dist/island/content.js (the
          // path the manifest's content_scripts entry references). It imports
          // nothing shared, so Rollup emits a single self-contained file with no
          // ESM chunk dependency — required because content scripts can't load
          // ESM imports.
          "island/content": resolve(root, "src/island/content.ts"),
        },
        output: {
          // Fixed, unhashed names so the manifest + HTML shells can reference them.
          entryFileNames: "[name].js",
          chunkFileNames: "chunks/[name].js",
          assetFileNames: "assets/[name].[ext]",
        },
      },
    },
    plugins: [copyStaticAssets(allowedWebBases)],
  };
});
