import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import vm from 'node:vm';

const distDir = path.resolve(process.cwd(), 'dist');
const assetsDir = path.join(distDir, 'assets');

async function fail(message) {
  process.stderr.write(`Deployment-path guard failed: ${message}\n`);
  process.exitCode = 1;
}

let assetNames;
try {
  assetNames = await readdir(assetsDir);
} catch (error) {
  await fail(`cannot read ${assetsDir}: ${error instanceof Error ? error.message : String(error)}`);
  assetNames = [];
}

const jsAssetNames = assetNames.filter((name) => name.endsWith('.js'));
const jsAssets = new Map(
  await Promise.all(
    jsAssetNames.map(async (name) => [name, await readFile(path.join(assetsDir, name), 'utf8')]),
  ),
);
const indexHtml = await readFile(path.join(distDir, 'index.html'), 'utf8').catch(() => '');
const entryMatch = indexHtml.match(/const entryAsset = ["']\.\/assets\/([^"']+\.js)["']/);
const entryName = entryMatch?.[1];
const preloadRuntime = [...jsAssets.entries()].find(([, source]) =>
  source.includes('modulepreload') && source.includes('new URL('),
);

if (!preloadRuntime) await fail('Vite module-preload runtime was not emitted.');
if (!entryName) await fail('portable runtime loader does not reference the Vite JavaScript entry.');
if (!indexHtml.includes('data-vibecanvas-runtime-assets')) {
  await fail('portable runtime asset loader was not emitted.');
}

const bootstrapSource = [...indexHtml.matchAll(/<script>([\s\S]*?)<\/script>/g)]
  .map((match) => match[1])
  .find((source) => source.includes('Resolve the reverse-proxy mount'));
if (!bootstrapSource) {
  await fail('runtime base-path bootstrap was not emitted.');
} else {
  const inferredBaseHref = (pathname) => {
    const appended = [];
    const window = {
      location: { pathname },
      __VIBECANVAS_RUNTIME_CONFIG__: {},
    };
    const document = {
      createElement: () => ({}),
      head: { appendChild: (element) => appended.push(element) },
    };
    vm.runInNewContext(bootstrapSource, { window, document });
    return appended[0]?.href;
  };
  const cases = [
    ['/embed/chat', '/'],
    ['/opaque/proxy/tasks/embed/chat', '/opaque/proxy/tasks/'],
    ['/login', '/'],
    ['/share/abcdefghijklmnopqrstuvwxyz0123456789abcdefg', '/'],
    ['/opaque/proxy/tasks/share/abcdefghijklmnopqrstuvwxyz0123456789abcdefg', '/opaque/proxy/tasks/'],
    ['/opaque/proxy/tasks/login', '/opaque/proxy/tasks/'],
    ['/workflow-executions/00000000-0000-0000-0000-000000000001', '/'],
    ['/opaque/proxy/workflow-executions/00000000-0000-0000-0000-000000000001', '/opaque/proxy/'],
    ['/knowledge', '/'],
    ['/opaque/proxy/knowledge', '/opaque/proxy/'],
    ['/preview', '/'],
    ['/opaque/proxy/preview', '/opaque/proxy/'],
    ['/permissions', '/'],
    ['/opaque/proxy/permissions', '/opaque/proxy/'],
    ['/management', '/'],
    ['/opaque/proxy/management', '/opaque/proxy/'],
    ['/mcp-servers/discover/official', '/'],
    ['/opaque/proxy/mcp-servers/discover/official', '/opaque/proxy/'],
  ];
  for (const [pathname, expected] of cases) {
    const actual = inferredBaseHref(pathname);
    if (actual !== expected) {
      await fail(
        `runtime base-path bootstrap resolved ${pathname} to ${String(actual)} instead of ${expected}.`,
      );
    }
  }
}
if (/<(?:script|link)\b[^>]*(?:src|href)=["']\.\/assets\//i.test(indexHtml)) {
  await fail('parser-discoverable relative asset tags remain in dist/index.html.');
}

if (preloadRuntime) {
  const [helperName, helper] = preloadRuntime;
  if (!helper.includes('new URL(')) {
    await fail(`${helperName} does not resolve module-preload URLs relative to the importing chunk.`);
  }
  if (/return[`'"]\/[`'"]\+/.test(helper)) {
    await fail(`${helperName} still forces module-preload dependencies to the origin root.`);
  }
}

if (entryName) {
  const entry = jsAssets.get(entryName) ?? '';
  if (!entry.includes('["./')) {
    await fail('dynamic dependency paths are not emitted as chunk-relative URLs.');
  }
  if (entry.includes('["assets/')) {
    await fail('dynamic dependency paths still target origin-root assets.');
  }
}

const routerVendorName = assetNames.find((name) => /^router-vendor-.*\.js$/.test(name));
const queryVendorName = assetNames.find((name) => /^query-vendor-.*\.js$/.test(name));
const appLayoutName = assetNames.find((name) => /^AppLayout-.*\.js$/.test(name));
if (!routerVendorName) await fail('react-router does not have a dedicated context-owner chunk.');
if (!queryVendorName) await fail('TanStack Query does not have a dedicated context-owner chunk.');
if (!appLayoutName) await fail('AppLayout chunk was not emitted.');
if (appLayoutName) {
  const appLayout = jsAssets.get(appLayoutName) ?? '';
  if (!appLayout.includes('./router-vendor-')) {
    await fail('AppLayout does not import hooks from the dedicated react-router chunk.');
  }
}

if (queryVendorName) {
  const queryContextOwners = [...jsAssets.entries()]
    .filter(([, source]) => source.includes('No QueryClient set'))
    .map(([name]) => name);
  if (queryContextOwners.length !== 1 || queryContextOwners[0] !== queryVendorName) {
    await fail(
      `QueryClient context escaped its dedicated chunk: ${queryContextOwners.join(', ') || 'missing'}.`,
    );
  }
}

if (!process.exitCode) {
  process.stdout.write('Deployment-path guard passed.\n');
}
