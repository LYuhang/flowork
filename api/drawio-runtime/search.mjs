#!/usr/bin/env node
/** Direct reuse of official search libraries. No MCP server or protocol. */
import { readFile, mkdir, rename, writeFile, stat } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildTagMap, searchShapesWithMeta } from '@drawio/mcp/src/shape-search.js';
import { searchShapesAndIcons, resolveIconServiceUrl } from '@drawio/mcp/src/icon-search.js';

if (process.argv.includes('--version')) {
  console.log('1.5.0');
  process.exit(0);
}
const indexUrl = process.env.DRAWIO_SHAPE_INDEX_URL ||
  'https://cdn.jsdelivr.net/gh/jgraph/drawio-mcp@main/shape-search/search-index.json';
const packageRoot = dirname(fileURLToPath(import.meta.resolve('@drawio/mcp/src/shape-search.js')));

function validateIndex(value) {
  if (!Array.isArray(value) || !value.length || value.some(item => !item || typeof item.style !== 'string' || typeof item.title !== 'string')) {
    throw new Error('The official shape index has an invalid format.');
  }
  return value;
}

async function loadIndex(warnings) {
  for (const path of [process.env.DRAWIO_SHAPE_INDEX_PATH, join(packageRoot, 'search-index.json')].filter(Boolean)) {
    try { return validateIndex(JSON.parse(await readFile(path, 'utf8'))); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  const cacheDir = '/memory/diagram-search-cache';
  const cache = join(cacheDir, createHash('sha256').update(indexUrl).digest('hex') + '.json');
  let cached;
  try {
    cached = validateIndex(JSON.parse(await readFile(cache, 'utf8')));
    if (Date.now() - (await stat(cache)).mtimeMs < 86400000) return cached;
  } catch { /* A missing/invalid cache is not evidence that no shapes match. */ }
  let index;
  try {
    const response = await fetch(indexUrl, { signal: AbortSignal.timeout(20000) });
    if (!response.ok) throw new Error(`Shape index HTTP ${response.status}.`);
    index = validateIndex(await response.json());
  } catch (error) {
    if (!cached) throw error;
    warnings.push('The public index could not be refreshed; results use the last valid cached index.');
    return cached;
  }
  try {
    await mkdir(cacheDir, { recursive: true });
    const temporary = `${cache}.${randomUUID()}.tmp`;
    await writeFile(temporary, JSON.stringify(index), { flag: 'wx', mode: 0o600 });
    await rename(temporary, cache);
  } catch { warnings.push('The shape index was loaded but could not be cached.'); }
  return index;
}

try {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const { query, limit = 10 } = JSON.parse(Buffer.concat(chunks).toString());
  if (typeof query !== 'string' || !query.trim() || !Number.isInteger(limit) || limit < 1 || limit > 50) {
    throw new Error('Provide non-empty query keywords and a limit between 1 and 50.');
  }
  const warnings = [];
  const index = await loadIndex(warnings);
  const tags = buildTagMap(index);
  const local = searchShapesWithMeta(index, tags, query, limit);
  const serviceUrl = resolveIconServiceUrl(process.env.DRAWIO_ICON_SERVICE_URL);
  let iconUnavailable = false;
  const nativeFetch = globalThis.fetch;
  globalThis.fetch = async (...args) => {
    try {
      const response = await nativeFetch(...args);
      if (!response.ok) iconUnavailable = true;
      return response;
    } catch (error) { iconUnavailable = true; throw error; }
  };
  const found = await searchShapesAndIcons(index, tags, query, limit, { serviceUrl });
  if (iconUnavailable) warnings.push('The optional icon service is unavailable; built-in shape results are still usable.');
  const shapes = found.map(({ title, style, w, h }) => ({ title, style, width: w, height: h }));
  console.log(JSON.stringify({ status: 'succeeded', query, shapes, warnings,
    message: shapes.length ? (local.strong ? 'Use the exact returned style in an XML attribute, then render to verify compatibility.' : 'Search includes approximate or icon matches. Check relevance and render the selected style before accepting it.') : 'No matching shapes found. Try broader keywords or use basic draw.io shapes.' }));
} catch (error) {
  console.log(JSON.stringify({ status: 'failed', error: 'shape_search_failed', message: error.message,
    hint: 'Check public index/network availability and retry, or use built-in basic geometric shapes. No document was changed.' }));
  process.exitCode = 1;
}
