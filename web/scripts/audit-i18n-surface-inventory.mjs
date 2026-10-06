import { readdir, readFile, stat } from 'node:fs/promises';
import { resolve } from 'node:path';

const manifest = JSON.parse(await readFile(
  resolve('e2e/fixtures/i18n-surface-inventory.json'),
  'utf8',
));
const routeAudit = await readFile(resolve('scripts/audit-route-inventory.mjs'), 'utf8');
const failures = [];
const routeIds = new Set(manifest.routes.map((route) => route.id));
const expectedStates = new Set([
  'loading',
  'empty',
  'error',
  'permission-denied',
  'disconnected',
  'destructive-action',
]);
const PRODUCT_ROOT = resolve('src');
const TAB_PATTERN = /<TabsTrigger\b|role\s*=\s*(?:\{\s*)?["']tab["']/g;
const DIALOG_PATTERN = /<(?:Alert)?DialogContent\b/g;

async function collectTsxFiles(directory) {
  const files = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const absolute = resolve(directory, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === '__tests__') continue;
      files.push(...await collectTsxFiles(absolute));
    } else if (entry.name.endsWith('.tsx') && !entry.name.endsWith('.test.tsx')) {
      files.push(absolute);
    }
  }
  return files;
}

function relativeSource(absolute) {
  return `src/${absolute.slice(PRODUCT_ROOT.length + 1).replaceAll('\\', '/')}`;
}

async function sourceExists(source) {
  try {
    return (await stat(resolve(source))).isFile();
  } catch {
    return false;
  }
}

if (manifest.locales.join(',') !== 'en,zh') failures.push('locales must be exactly en and zh');
if (!manifest.viewports.some((viewport) => viewport.width === 560)) {
  failures.push('inventory must include the required 560px viewport');
}
if (routeIds.size !== manifest.routes.length) failures.push('route ids must be unique');
const expectedRouteIds = [...routeAudit.matchAll(/\['[^']+', '([^']+)'\]/g)].map(match => match[1]);
for (const id of expectedRouteIds) {
  if (!routeIds.has(id)) failures.push(`route missing from locale inventory: ${id}`);
}

for (const route of manifest.routes) {
  if (!routeAudit.includes(`'${route.id}'`)) {
    failures.push(`route ${route.id} is absent from audit-route-inventory.mjs`);
  }
}

for (const groupName of ['childTabs', 'modals']) {
  const ids = new Set();
  for (const surface of manifest[groupName]) {
    if (ids.has(surface.id)) failures.push(`${groupName} id ${surface.id} is duplicated`);
    ids.add(surface.id);
    const referencedRoutes = surface.routeIds ?? [surface.routeId];
    if (!referencedRoutes.length) {
      failures.push(`${groupName} ${surface.id} has no route reference`);
    }
    for (const routeId of referencedRoutes) {
      if (!routeIds.has(routeId)) {
        failures.push(`${groupName} ${surface.id} references unknown route ${routeId}`);
      }
    }
    if (!['automated', 'focused-component', 'visible-browser'].includes(surface.evidence)) {
      failures.push(`${groupName} ${surface.id} has invalid evidence ${surface.evidence}`);
    }
    for (const conditional of surface.conditionalTabs ?? []) {
      if (!surface.tabs?.includes(conditional)) {
        failures.push(`${groupName} ${surface.id} has unknown conditional tab ${conditional}`);
      }
    }
    for (const fixtureTab of surface.fixtureTabs ?? []) {
      if (!surface.tabs?.includes(fixtureTab)) {
        failures.push(`${groupName} ${surface.id} has unknown fixture tab ${fixtureTab}`);
      }
    }
    if (!surface.source || !await sourceExists(surface.source)) {
      failures.push(`${groupName} ${surface.id} source does not exist: ${surface.source ?? '(missing)'}`);
    }
    if (groupName === 'modals') {
      if (!Number.isInteger(surface.dialogCount) || surface.dialogCount < 0) {
        failures.push(`modal ${surface.id} has invalid dialogCount ${surface.dialogCount}`);
      }
      if (!surface.variants?.length) failures.push(`modal ${surface.id} has no variants`);
      if (surface.dialogCount > 0 && surface.variants?.length !== surface.dialogCount) {
        failures.push(
          `modal ${surface.id} declares ${surface.dialogCount} DialogContent nodes but `
          + `${surface.variants?.length ?? 0} variants`,
        );
      }
    }
  }
}

const productFiles = await collectTsxFiles(PRODUCT_ROOT);
const tabSourcesInCode = new Set();
const dialogCountsInCode = new Map();
for (const absolute of productFiles) {
  const source = relativeSource(absolute);
  const contents = await readFile(absolute, 'utf8');
  TAB_PATTERN.lastIndex = 0;
  DIALOG_PATTERN.lastIndex = 0;
  if (TAB_PATTERN.test(contents)) tabSourcesInCode.add(source);
  const dialogCount = Array.from(contents.matchAll(DIALOG_PATTERN)).length;
  if (dialogCount > 0) dialogCountsInCode.set(source, dialogCount);
}

const inventoriedTabSources = new Set(manifest.childTabs.map((surface) => surface.source));
for (const source of tabSourcesInCode) {
  if (!inventoriedTabSources.has(source)) failures.push(`child-tab source missing from inventory: ${source}`);
}
for (const source of inventoriedTabSources) {
  if (!tabSourcesInCode.has(source)) failures.push(`stale child-tab source in inventory: ${source}`);
}

const primitiveSources = new Set();
for (const primitive of manifest.modalPrimitives ?? []) {
  if (!primitive.reason?.trim()) failures.push(`modal primitive ${primitive.source} has no reason`);
  if (!await sourceExists(primitive.source)) failures.push(`modal primitive source does not exist: ${primitive.source}`);
  primitiveSources.add(primitive.source);
}
const inventoriedDialogCounts = new Map();
for (const surface of manifest.modals) {
  if (surface.dialogCount === 0) continue;
  inventoriedDialogCounts.set(
    surface.source,
    (inventoriedDialogCounts.get(surface.source) ?? 0) + surface.dialogCount,
  );
}
for (const [source, count] of dialogCountsInCode) {
  if (primitiveSources.has(source)) continue;
  const expected = inventoriedDialogCounts.get(source);
  if (expected === undefined) failures.push(`modal source missing from inventory: ${source}`);
  else if (expected !== count) {
    failures.push(`modal source ${source} has ${count} DialogContent nodes; inventory declares ${expected}`);
  }
}
for (const [source, count] of inventoriedDialogCounts) {
  const actual = dialogCountsInCode.get(source);
  if (actual === undefined) failures.push(`stale modal source in inventory: ${source}`);
  else if (actual !== count) failures.push(`modal source ${source} inventory/code count mismatch: ${count}/${actual}`);
}

const stateIds = new Set();
for (const state of manifest.dynamicStates) {
  stateIds.add(state.id);
  if (!state.routeIds.length) failures.push(`dynamic state ${state.id} has no routes`);
  for (const routeId of state.routeIds) {
    if (!routeIds.has(routeId)) failures.push(`dynamic state ${state.id} references unknown route ${routeId}`);
  }
  if (!state.componentSpecs?.length) failures.push(`dynamic state ${state.id} has no component evidence`);
}
for (const expected of expectedStates) {
  if (!stateIds.has(expected)) failures.push(`dynamic-state inventory missing ${expected}`);
}

const componentSpecs = new Set();
for (const source of manifest.componentSpecs ?? []) {
  if (componentSpecs.has(source)) failures.push(`component spec is duplicated: ${source}`);
  componentSpecs.add(source);
  if (!await sourceExists(source)) failures.push(`component spec does not exist: ${source}`);
  if (!/\.test\.tsx?$/.test(source)) failures.push(`component spec is not a test file: ${source}`);
}
if (!componentSpecs.size) failures.push('component spec matrix is empty');
for (const state of manifest.dynamicStates) {
  for (const source of state.componentSpecs ?? []) {
    if (!componentSpecs.has(source)) {
      failures.push(`dynamic state ${state.id} references a component spec outside the matrix: ${source}`);
    }
  }
}

if (failures.length) {
  process.stderr.write(`${failures.join('\n')}\n`);
  process.exitCode = 1;
} else {
  process.stdout.write(
    `i18n surface inventory passed: ${manifest.routes.length} routes, `
    + `${manifest.childTabs.length} child-tab groups, ${manifest.modals.length} modals, `
    + `${manifest.dynamicStates.length} dynamic-state groups, `
    + `${componentSpecs.size} component specs.\n`,
  );
}
