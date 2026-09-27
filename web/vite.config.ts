import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { visualizer } from 'rollup-plugin-visualizer';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { interactiveBootstrapScriptSource } from './src/components/agent-sidebar/tool-render/interactive-html-runtime';

// `rollup-plugin-visualizer` writes `dist/stats.html` so we can audit which
// modules dominate the bundle. We disable `open` (no DISPLAY in CI/devbox)
// and set `gzipSize` so the report reflects what the user actually downloads.
// Compatible with Vite 8 / Rolldown — it consumes the standard Rollup
// plugin hooks (`writeBundle`).
//
// Production builds DO NOT ship the visualizer: nginx would otherwise
// serve `dist/stats.html` (module names, sizes, source paths) to anyone
// who hits `/stats.html`. Gate on `ANALYZE=1` so `pnpm build:analyze`
// opts in explicitly while regular `pnpm build` keeps it out.
const enableVisualizer = process.env.ANALYZE === '1';
const nativeApiTarget = `http://127.0.0.1:${process.env.API_PORT || '8000'}`;

// Extra dev/preview hosts are deployment-owned. Values are comma-separated
// host names; a leading dot allows the domain and all of its subdomains,
// matching Vite's allowedHosts semantics. `launch.sh` derives the exact host
// from VIBECANVAS_PUBLIC_URL unless WEB_ALLOWED_HOSTS is explicitly supplied.
// Never use `true`, which would disable DNS-rebinding protection.
function normalizeAllowedHost(value: string): string {
  const candidate = value.trim();
  if (!candidate) return '';
  if (candidate.startsWith('.')) return candidate;
  try {
    const url = new URL(candidate.includes('://') ? candidate : `http://${candidate}`);
    return url.hostname;
  } catch {
    return candidate;
  }
}

const allowedHosts = [
  'localhost',
  ...(process.env.WEB_ALLOWED_HOSTS ?? '')
    .split(',')
    .map(normalizeAllowedHost)
    .filter(Boolean),
];

// Allow the extension side panel to frame the embedded web route.
// The embed (`/embed/*`) is rendered inside a `chrome-extension://` top-level
// document, so the host must NOT send `X-Frame-Options: DENY` and MUST allow
// `chrome-extension://` as a `frame-ancestors` source. The app ships no CSP
// today (verified — see HtmlPreview.tsx), so there is no `DENY`/`frame-ancestors`
// to remove; we only ADD the permissive directive for local dev/preview.
//
// DEV-PERMISSIVE: `chrome-extension://*` allows ANY unpacked extension to frame
// the dev server (the extension id is unstable across reloads in dev). In PROD
// the host MUST pin the real id from `VITE_EXTENSION_ID` — see the prod note in
// `web/nginx.conf` (frame-ancestors 'self' chrome-extension://<EXT_ID>).
const EMBED_FRAME_ANCESTORS_CSP =
  "frame-ancestors 'self' chrome-extension://*";

/**
 * Vite normally writes parser-discoverable `./assets/*` tags into index.html.
 * A dynamically inserted `<base>` is too late for Chromium's speculative HTML
 * scanner: on a direct `/workflow/:id` reload it can first request
 * `/workflow/assets/*` before the bootstrap script establishes the real mount.
 *
 * Keep the build portable by replacing those generated tags with one inline
 * module loader. Inline script execution observes the runtime `<base>` above,
 * so every stylesheet, preload and the entry module starts from the resolved
 * deployment mount without a burst of doomed nested-route requests.
 */
function portableRuntimeAssetsPlugin() {
  let cspScriptHashes = '';
  return {
    name: 'vibecanvas-portable-runtime-assets',
    apply: 'build' as const,
    enforce: 'post' as const,
    transformIndexHtml: {
      order: 'post' as const,
      handler(html: string) {
        let entryAsset = '';
        const stylesheetAssets: string[] = [];
        const preloadAssets: string[] = [];
        const assetTag = /<script\b[^>]*\bsrc=["']\.\/assets\/[^"']+["'][^>]*><\/script>|<link\b[^>]*\bhref=["']\.\/assets\/[^"']+["'][^>]*>/gi;
        const stripped = html.replace(assetTag, (tag) => {
          const asset = tag.match(/(?:src|href)=["'](\.\/assets\/[^"']+)["']/i)?.[1];
          if (!asset) return tag;
          if (/^<script\b/i.test(tag) && /\btype=["']module["']/i.test(tag)) {
            entryAsset = asset;
            return '';
          }
          if (/\brel=["']stylesheet["']/i.test(tag)) {
            stylesheetAssets.push(asset);
            return '';
          }
          if (/\brel=["']modulepreload["']/i.test(tag)) {
            preloadAssets.push(asset);
            return '';
          }
          return tag;
        });
        if (!entryAsset) {
          throw new Error('portable runtime asset loader could not find the Vite entry module');
        }
        const loader = `
    <script type="module" data-vibecanvas-runtime-assets>
      const entryAsset = ${JSON.stringify(entryAsset)};
      const stylesheetAssets = ${JSON.stringify(stylesheetAssets)};
      const preloadAssets = ${JSON.stringify(preloadAssets)};
      const resolveAsset = (asset) => new URL(asset, document.baseURI).href;
      for (const asset of preloadAssets) {
        const link = document.createElement('link');
        link.rel = 'modulepreload';
        link.crossOrigin = 'anonymous';
        link.href = resolveAsset(asset);
        document.head.appendChild(link);
      }
      await Promise.all(stylesheetAssets.map((asset) => new Promise((resolve, reject) => {
        const link = document.createElement('link');
        link.rel = 'stylesheet';
        link.crossOrigin = 'anonymous';
        link.href = resolveAsset(asset);
        link.onload = resolve;
        link.onerror = () => reject(new Error('Failed to load stylesheet: ' + asset));
        document.head.appendChild(link);
      })));
      await import(resolveAsset(entryAsset));
    </script>`;
        const withLoader = stripped.replace('</head>', `${loader}\n  </head>`);
        const hashes = Array.from(
          withLoader.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi),
          (match) =>
            `'sha256-${createHash('sha256').update(match[1]).digest('base64')}'`,
        );
        hashes.push(
          `'sha256-${createHash('sha256').update(interactiveBootstrapScriptSource()).digest('base64')}'`,
        );
        cspScriptHashes = Array.from(new Set(hashes)).join(' ');
        const policy = [
          "default-src 'self'",
          `script-src 'self' ${cspScriptHashes}`,
          "script-src-attr 'none'",
          "style-src 'self' 'unsafe-inline'",
          "img-src 'self' data: blob:",
          "font-src 'self' data:",
          "connect-src 'self'",
          "worker-src 'self' blob:",
          "frame-src 'self' data: blob: http: https:",
          "media-src 'self' data: blob:",
          "object-src 'none'",
          "base-uri 'self'",
          "form-action 'self'",
          "manifest-src 'self'",
        ].join('; ');
        return withLoader.replace(
          '<title>',
          `<meta http-equiv="Content-Security-Policy" content="${policy}" />\n    <title>`,
        );
      },
    },
    generateBundle() {
      if (!cspScriptHashes) {
        throw new Error('portable runtime asset loader did not produce CSP hashes');
      }
      this.emitFile({
        type: 'asset',
        fileName: 'csp-script-hashes.txt',
        source: `${cspScriptHashes}\n`,
      });
    },
  };
}

export default defineConfig({
  // HTML entry assets are resolved against the runtime <base> injected by
  // index.html.  This keeps one build portable across localhost, a temporary
  // workspace-proxy prefix, and a production mount without hard-coded URLs.
  base: './',
  plugins: [
    react(),
    portableRuntimeAssetsPlugin(),
    enableVisualizer &&
      visualizer({
        filename: 'dist/stats.html',
        open: false,
        gzipSize: true,
        brotliSize: true,
      }),
  ].filter(Boolean),
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  server: {
    port: 5173,
    // Let the extension side panel frame the dev server so
    // `/embed/*` loads in the iframe. Vite applies these to ALL dev responses;
    // that is fine in dev (the directive only LOOSENS framing, never tightens
    // it). In prod the header is scoped to `/embed/*` only — see `nginx.conf`.
    headers: { 'Content-Security-Policy': EMBED_FRAME_ANCESTORS_CSP },
    // Allow access through corp jump-host / workspace proxies (Vite 5 blocks
    // unknown Host headers by default). A leading-dot entry matches the domain
    // and all its subdomains. Dev/preview only.
    allowedHosts,
    proxy: {
      // Keep the local proxy on the same IPv4 interface as native_dev_up.sh.
      // Node 24 may resolve `localhost` to ::1 first while uvicorn listens on
      // 127.0.0.1, which otherwise turns every browser API request into 502.
      '/api': { target: nativeApiTarget, changeOrigin: true, ws: true },
      '/healthz': { target: nativeApiTarget, changeOrigin: true },
    },
  },
  preview: {
    port: 5173,
    // Same dev-permissive framing header for `vite preview` (used to smoke-test
    // the production build locally). Prod scopes this to `/embed/*` — see nginx.
    headers: { 'Content-Security-Policy': EMBED_FRAME_ANCESTORS_CSP },
    allowedHosts,
    proxy: {
      '/api': { target: nativeApiTarget, changeOrigin: true, ws: true },
      '/healthz': { target: nativeApiTarget, changeOrigin: true },
    },
  },
  // The application can be mounted below an arbitrary reverse-proxy prefix
  // (for example a short-lived workspace URL) or at `/` in production. Keep
  // URLs emitted *inside JavaScript chunks* relative to their importing chunk
  // so Vite's module-preload helper preserves whatever prefix the entry chunk
  // was fetched from. A static `base: '/some-prefix/'` cannot model this: the
  // prefix is deployment/runtime-owned and may change between sessions.
  //
  // The runtime <base> in index.html is what makes the globally relative
  // build safe on direct reloads of nested routes; runtime JS dependencies
  // remain chunk-relative as a second layer of protection.
  experimental: {
    renderBuiltUrl(_filename, { hostType }) {
      if (hostType === 'js') return { relative: true };
      return undefined;
    },
  },
  build: {
    // The public repository is the authoritative source. Omitting maps keeps
    // release images smaller and avoids exposing unpublished dirty-tree input.
    sourcemap: false,
    rollupOptions: {
      output: {
        // Vite 8 / Rolldown requires function form
        manualChunks(id) {
          // Keep React itself independent from React Flow. Without this rule,
          // Rolldown can hoist React into the manually named `xyflow` chunk,
          // making every route preload the canvas runtime even when no canvas
          // is visible.
          if (
            /node_modules\/\.pnpm\/(?:react|react-dom|scheduler|use-sync-external-store)@/.test(id)
          ) {
            return 'react-vendor';
          }
          // Router contexts and hooks must come from one stable, cycle-free
          // module. If react-router stays in the entry chunk, lazy layout
          // chunks import their hooks back from that entry (`entry -> layout
          // -> entry`). Besides making startup harder to reason about, this
          // has proven fragile when a path proxy evaluates chunks through its
          // own loader. Give the RouterProvider and every lazy route the same
          // dedicated context owner.
          if (/node_modules\/\.pnpm\/react-router@/.test(id)) {
            return 'router-vendor';
          }
          // QueryClientProvider and every query hook must share one Context
          // owner. Letting Rolldown keep the provider in the entry while
          // placing useQuery/useMutation in lazy chunks creates
          // `entry -> AppLayout -> hook -> entry`; that cycle has produced a
          // false "No QueryClient set" behind deployment proxies. Keep both
          // react-query and query-core together so hooks never import their
          // context back from the application entry.
          if (
            /node_modules\/\.pnpm\/@tanstack\+(?:react-query|query-core)@/.test(id)
          ) {
            return 'query-vendor';
          }
          // Lucide exposes every icon as a tiny module. Rolldown otherwise
          // emits dozens of 100-500 byte requests shared by the shell and
          // lazy pages. That is cheap on localhost but expensive behind a
          // jump-host where per-request latency dominates transfer time.
          // Tree-shaking still keeps unused icons out of this consolidated
          // chunk; only icons referenced by the application are included.
          if (/node_modules\/\.pnpm\/lucide-react@/.test(id)) {
            return 'icons-vendor';
          }
          // The application stores and React Flow both use Zustand. Keep the
          // shared state runtime neutral; otherwise the shell's auth/chat
          // stores make the browser preload the entire manually named canvas
          // chunk before a workflow or workflow preview is opened.
          if (/node_modules\/\.pnpm\/zustand@/.test(id)) return 'state-vendor';
          // React Flow and the app both depend on clsx. Give that shared leaf
          // an explicit neutral owner so Rolldown does not place it inside the
          // xyflow chunk and pull the whole canvas into the app shell.
          if (/node_modules\/\.pnpm\/clsx@/.test(id)) return 'ui-utils';
          if (id.includes('node_modules/@xyflow')) return 'xyflow';
        },
      },
    },
  },
});
