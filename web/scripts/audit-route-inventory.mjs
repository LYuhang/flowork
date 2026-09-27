import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const router = await readFile(resolve('src/app/router.tsx'), 'utf8');
const visualMatrix = await readFile(resolve('e2e/15-route-visual-matrix.spec.ts'), 'utf8');

const inventory = [
  ['/', 'root'],
  ['embed/chat', 'embed-chat'],
  ['login', 'login'],
  ['signup', 'signup'],
  ['reset-password', 'reset-password'],
  ['chat', 'chat'],
  ['share/:token', 'shared-chat-error'],
  ['preview', 'standalone-preview-error'],
  ['workspace', 'workspace'],
  ['management', 'management'],
  ['tasks', 'tasks'],
  ['tasks/:taskId', 'task-detail-error'],
  ['deployments', 'deployments'],
  ['deployments/:depId', 'deployment-detail-error'],
  ['credentials', 'credentials'],
  ['mcp-servers', 'mcp-servers'],
  ['mcp-servers/discover/:source', 'mcp-catalog-detail-error'],
  ['mcp-servers/:id', 'mcp-detail-error'],
  ['skills', 'skills'],
  ['skills/discover/:source', 'skill-catalog-detail-error'],
  ['skills/:id', 'skill-detail-error'],
  ['storage', 'storage'],
  ['knowledge', 'knowledge'],
  ['knowledge/:kbId', 'knowledge-detail-error'],
  ['workflow/:wfId', 'workflow'],
  ['workflow/:wfId/version/:vKey', 'workflow-version'],
  ['settings', 'settings'],
  ['settings/openrouter/callback/:openrouterState', 'openrouter-callback-error'],
];

const failures = [];
const normalizeRoute = (route) => route === '/' ? '/' : route.replace(/^\/+/, '');
const routerRoutes = new Set(
  Array.from(router.matchAll(/\bpath:\s*['"]([^'"]+)['"]/g), (match) =>
    normalizeRoute(match[1]),
  ),
);
const inventoriedRoutes = new Set(
  inventory.map(([route]) => normalizeRoute(route)),
);

for (const [route, visualId] of inventory) {
  if (!routerRoutes.has(normalizeRoute(route))) failures.push(`router missing ${route}`);
  if (!visualMatrix.includes(`id: '${visualId}'`)) failures.push(`visual matrix missing ${visualId}`);
}
for (const route of routerRoutes) {
  if (!inventoriedRoutes.has(route)) failures.push(`inventory missing router route ${route}`);
}
if (!visualMatrix.includes("test('mobile shell and primary routes at 390px'")) {
  failures.push('visual matrix missing mobile representative gate');
}

if (failures.length) {
  process.stderr.write(`${failures.join('\n')}\n`);
  process.exitCode = 1;
} else {
  process.stdout.write(`Route inventory passed: ${inventory.length} production routes covered.\n`);
}
