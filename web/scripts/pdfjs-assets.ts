import { readFileSync, readdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import type { Plugin } from 'vite';

/** PDF.js needs external character maps and fonts even when its worker loads.
 * Ship the locked package's resources locally, versioned alongside the worker.
 * Only enumerated package files are exposed by the development middleware. */
export function pdfjsAssetsPlugin(): Plugin {
  const require = createRequire(import.meta.url);
  const root = path.dirname(require.resolve('pdfjs-dist/package.json'));
  const { version } = JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8')) as { version: string };
  const resources = new Map<string, string>();
  for (const directory of ['cmaps', 'standard_fonts', 'wasm', 'iccs']) {
    for (const entry of readdirSync(path.join(root, directory), { withFileTypes: true })) {
      if (!entry.isFile()) continue;
      resources.set(`assets/pdfjs/${version}/${directory}/${entry.name}`, path.join(root, directory, entry.name));
    }
  }
  return {
    name: 'flowork-pdfjs-assets',
    generateBundle() {
      for (const [fileName, filename] of resources) {
        this.emitFile({ type: 'asset', fileName, source: readFileSync(filename) });
      }
    },
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const pathname = new URL(request.url ?? '/', 'http://localhost').pathname.slice(1);
        const filename = resources.get(pathname);
        if (!filename) return next();
        const contentType = filename.endsWith('.wasm') ? 'application/wasm'
          : filename.endsWith('.js') ? 'text/javascript' : 'application/octet-stream';
        response.setHeader('Content-Type', contentType);
        response.end(readFileSync(filename));
      });
    },
  };
}
