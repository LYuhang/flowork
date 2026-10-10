import { readFile, readdir, stat } from 'node:fs/promises';
import { gzipSync } from 'node:zlib';
import { resolve } from 'node:path';

const dist = resolve('dist');
const assets = resolve(dist, 'assets');
const files = (await readdir(assets)).filter((name) => /\.(?:js|mjs)$/.test(name));
const rows = await Promise.all(files.map(async (name) => {
  const path = resolve(assets, name);
  const bytes = (await stat(path)).size;
  const gzip = gzipSync(await readFile(path)).byteLength;
  return { name, bytes, gzip };
}));
// ELK is an on-demand computation worker, fetched only after the layout action.
// Keep the existing application budget; give this one pinned worker its own cap.
const layoutWorkers = rows.filter(row => /^elk-worker\.min-.*\.js$/.test(row.name));
const applicationRows = rows.filter(row => !layoutWorkers.includes(row));
const total = rows.reduce((sum, row) => sum + row.bytes, 0);
const applicationTotal = applicationRows.reduce((sum, row) => sum + row.bytes, 0);
const largest = [...applicationRows].sort((a, b) => b.bytes - a.bytes)[0];
const index = await readFile(resolve(dist, 'index.html'), 'utf8');
const entryName = index.match(/\.\/assets\/(index-[^"']+\.js)/)?.[1];
const entry = rows.find((row) => row.name === entryName);

const failures = [];
if (!entry) failures.push('could not identify the production entry chunk');
if (entry && entry.gzip > 180_000) failures.push(`entry gzip ${entry.gzip} exceeds 180000 bytes`);
if (largest && largest.bytes > 1_500_000) failures.push(`${largest.name} exceeds 1.5 MB`);
if (applicationTotal > 21_000_000) failures.push(`application lazy JavaScript ${applicationTotal} exceeds 21 MB`);
if (layoutWorkers.length > 1 || layoutWorkers.some(row => row.bytes > 1_700_000)) failures.push('layout worker exceeds its separate 1.7 MB budget');
if (!rows.some((row) => /^ChatPage-/.test(row.name))) failures.push('ChatPage is not route-split');
if (!rows.some((row) => /^CanvasPage-/.test(row.name))) failures.push('CanvasPage is not route-split');

const summary = `Bundle budget: ${rows.length} chunks, ${(total / 1_000_000).toFixed(2)} MB lazy total (${layoutWorkers.length} layout worker), entry ${entry ? `${(entry.gzip / 1000).toFixed(1)} kB gzip` : 'missing'}, largest ${largest ? `${largest.name} ${(largest.bytes / 1_000_000).toFixed(2)} MB` : 'missing'}.`;
if (failures.length) {
  process.stderr.write(`${summary}\n${failures.join('\n')}\n`);
  process.exitCode = 1;
} else {
  process.stdout.write(`${summary}\n`);
}
