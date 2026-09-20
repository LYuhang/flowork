#!/usr/bin/env node

/**
 * Thin Flowork adapter around the official @drawio/mcp server.
 *
 * Upstream tools are proxied without changing their names, schemas, or
 * results. Flowork adds only an atomic /data file boundary and a lightweight
 * validation entry point so draw.io XML participates in the ordinary
 * Sandbox <-> VFS lifecycle.
 */

import { createHash } from 'node:crypto';
import { mkdir, open, rename, rm } from 'node:fs/promises';
import { basename, dirname, resolve } from 'node:path';
import process from 'node:process';

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';
import { routeXml } from '@drawio/mcp/src/libavoid-pass.js';

const VERSION = '1.5.0';
const MAX_SOURCE_BYTES = 8 * 1024 * 1024;
const ROUTING_CORE_URL =
  'https://viewer.diagrams.net/js/libavoid-js/libavoid-routing.js';

if (process.argv.includes('--version') || process.argv.includes('-v')) {
  process.stdout.write(`${VERSION}\n`);
  process.exit(0);
}

if (process.argv.includes('--help') || process.argv.includes('-h')) {
  process.stdout.write(
    `flowork-diagram-mcp ${VERSION}\n\n` +
    'Official draw.io MCP with the Flowork sandbox file adapter.\n',
  );
  process.exit(0);
}

// The official route pass normally revalidates its bundled routing core with
// the draw.io CDN. A Chat sandbox must be deterministic and work offline, so
// disable only that refresh request; routeXml then immediately uses the exact
// official copy vendored in @drawio/mcp. Other upstream requests, including
// shape search, retain their original behavior.
const upstreamFetch = globalThis.fetch;
globalThis.fetch = async (input, init) => {
  const url = typeof input === 'string' ? input : input?.url;
  if (url === ROUTING_CORE_URL) {
    throw new Error('using the official vendored draw.io routing core');
  }
  return upstreamFetch(input, init);
};

const SAVE_TOOL = {
  name: 'save_drawio_file',
  description:
    'Atomically save native draw.io XML below /data/diagrams. This is a thin ' +
    'Flowork file adapter around the official draw.io format. Optionally run ' +
    'the official vendored Libavoid pass before saving. After success, render ' +
    'the returned source revision with the provided official draw.io CLI argv, ' +
    'inspect its PNG with the Runtime image-view capability, then publish the ' +
    'accepted native file with render_interactive.',
  inputSchema: {
    type: 'object',
    additionalProperties: false,
    properties: {
      path: {
        type: 'string',
        pattern: '^/data/diagrams/[A-Za-z0-9][A-Za-z0-9._-]{0,95}\\.drawio$',
        description: 'Destination native draw.io file.',
      },
      content: {
        type: 'string',
        minLength: 1,
        description: 'A complete mxGraphModel or mxfile document.',
      },
      routing: {
        type: 'string',
        enum: ['libavoid'],
        description:
          'Optional official obstacle-avoiding orthogonal routing pass.',
      },
    },
    required: ['path', 'content'],
  },
  annotations: {
    title: 'Save draw.io File',
    readOnlyHint: false,
    destructiveHint: false,
    idempotentHint: true,
    openWorldHint: false,
  },
};

function textResult(value, isError = false) {
  const text = JSON.stringify(value);
  return { content: [{ type: 'text', text }], structuredContent: value, isError };
}

function validateSource(content) {
  const bytes = Buffer.byteLength(content);
  const errors = [];
  if (bytes > MAX_SOURCE_BYTES) errors.push('source exceeds the 8 MiB limit');
  if (/<!DOCTYPE|<!ENTITY/i.test(content)) {
    errors.push('DOCTYPE and ENTITY declarations are not allowed');
  }
  if (!/<(?:mxGraphModel|mxfile)\b/.test(content)) {
    errors.push('expected an mxGraphModel or mxfile root element');
  }
  const ids = [...content.matchAll(/<mxCell\b[^>]*\bid="([^"]+)"/g)]
    .map((match) => match[1]);
  const duplicateIds = [...new Set(ids.filter((id, index) => ids.indexOf(id) !== index))];
  if (duplicateIds.length) {
    errors.push(`duplicate mxCell IDs: ${duplicateIds.slice(0, 10).join(', ')}`);
  }
  const idSet = new Set(ids);
  const danglingTerminals = [];
  for (const match of content.matchAll(/<mxCell\b([^>]*\bedge="1"[^>]*)>/g)) {
    const edgeId = /\bid="([^"]+)"/.exec(match[1])?.[1] ?? '(unknown)';
    for (const terminal of ['source', 'target']) {
      const ref = new RegExp(`\\b${terminal}="([^"]+)"`).exec(match[1])?.[1];
      if (ref && !idSet.has(ref)) danglingTerminals.push(`${edgeId}.${terminal}=${ref}`);
    }
  }
  if (danglingTerminals.length) {
    errors.push(`dangling edge terminals: ${danglingTerminals.slice(0, 10).join(', ')}`);
  }
  return {
    bytes,
    cells: ids.length,
    edges: [...content.matchAll(/<mxCell\b[^>]*\bedge="1"/g)].length,
    vertices: [...content.matchAll(/<mxCell\b[^>]*\bvertex="1"/g)].length,
    errors,
  };
}

function physicalPath(publicPath) {
  if (!/^\/data\/diagrams\/[A-Za-z0-9][A-Za-z0-9._-]{0,95}\.drawio$/.test(publicPath)) {
    throw new Error('path must be a safe .drawio file below /data/diagrams');
  }
  const dataRoot = resolve(process.env.FLOWORK_SANDBOX_DATA_ROOT || '/data');
  const candidate = resolve(dataRoot, publicPath.slice('/data/'.length));
  const allowed = resolve(dataRoot, 'diagrams');
  if (!candidate.startsWith(`${allowed}/`)) throw new Error('path escapes /data/diagrams');
  return candidate;
}

function feedbackPath(publicPath, contentHash) {
  const identity = createHash('sha256').update(publicPath).digest('hex').slice(0, 20);
  const revision = contentHash.replace(/^sha256:/, '').slice(0, 20);
  return `/memory/diagram-feedback/${identity}-${revision}.png`;
}

function physicalFeedbackPath(publicPath) {
  if (!/^\/memory\/diagram-feedback\/[a-f0-9]{20}-[a-f0-9]{20}\.png$/.test(publicPath)) {
    throw new Error('feedback path must be a source-bound PNG below /memory');
  }
  const memoryRoot = resolve(process.env.FLOWORK_SANDBOX_MEMORY_ROOT || '/memory');
  const candidate = resolve(memoryRoot, publicPath.slice('/memory/'.length));
  if (!candidate.startsWith(`${memoryRoot}/`)) throw new Error('feedback path escapes /memory');
  return candidate;
}

async function atomicWrite(path, content) {
  await mkdir(dirname(path), { recursive: true });
  const temporary = resolve(
    dirname(path),
    `.${basename(path)}.${process.pid}.${Date.now()}.tmp`,
  );
  try {
    const handle = await open(temporary, 'wx', 0o600);
    try {
      await handle.writeFile(content, 'utf8');
      await handle.sync();
    } finally {
      await handle.close();
    }
    await rename(temporary, path);
    const directory = await open(dirname(path), 'r');
    try {
      await directory.sync();
    } finally {
      await directory.close();
    }
  } finally {
    await rm(temporary, { force: true });
  }
}

const upstreamEntry = new URL(
  './node_modules/@drawio/mcp/src/index.js',
  import.meta.url,
);
const upstreamTransport = new StdioClientTransport({
  command: process.execPath,
  args: [upstreamEntry.pathname],
  cwd: process.cwd(),
  env: {
    PATH: process.env.PATH || '',
    HOME: process.env.HOME || '/tmp',
    DRAWIO_BASE_URL: process.env.DRAWIO_BASE_URL || 'https://app.diagrams.net/',
    DRAWIO_ICON_SERVICE_URL: process.env.DRAWIO_ICON_SERVICE_URL || 'off',
  },
  stderr: 'inherit',
});
const upstream = new Client({ name: 'flowork-drawio-adapter', version: VERSION });
await upstream.connect(upstreamTransport);
const upstreamTools = (await upstream.listTools()).tools;
const upstreamNames = new Set(upstreamTools.map((tool) => tool.name));

const server = new Server(
  { name: 'flowork-drawio', version: VERSION },
  { capabilities: { tools: {} }, instructions:
      'Official draw.io MCP with ordinary /data file persistence for Flowork.' },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [...upstreamTools, SAVE_TOOL],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args = {} } = request.params;
  try {
    if (upstreamNames.has(name)) {
      return await upstream.callTool({ name, arguments: args });
    }
    if (name === 'save_drawio_file') {
      let content = args.content;
      if (typeof content !== 'string') {
        return textResult({ status: 'failed', errors: ['content must be a string'] }, true);
      }
      const initial = validateSource(content);
      if (initial.errors.length) {
        return textResult({ status: 'failed', ...initial }, true);
      }
      if (args.routing === 'libavoid') content = await routeXml(content);
      const inspected = validateSource(content);
      if (inspected.errors.length) {
        return textResult({ status: 'failed', ...inspected }, true);
      }
      const path = String(args.path || '');
      const destination = physicalPath(path);
      await atomicWrite(destination, content);
      const contentHash = `sha256:${createHash('sha256').update(content).digest('hex')}`;
      const renderedPath = feedbackPath(path, contentHash);
      await mkdir(dirname(physicalFeedbackPath(renderedPath)), { recursive: true });
      const renderArgv = [
        '/usr/local/bin/flowork-drawio-export',
        '-x',
        '-f',
        'png',
        '-e',
        '-b',
        '16',
        '-s',
        '2',
        '-o',
        renderedPath,
        path,
      ];
      return textResult({
        status: 'ready',
        fileRef: { path, contentHash },
        routing: args.routing || null,
        inspection: inspected,
        visualFeedback: {
          status: 'render_required',
          path: renderedPath,
          sourceHash: contentHash,
          renderer: 'official-drawio-desktop-cli',
          argv: renderArgv,
        },
        nextActions: [
          {
            action: 'run_command',
            tool: 'shell',
            request: { argv: renderArgv },
          },
          {
            action: 'inspect_image',
            path: renderedPath,
            runtimeTools: {
              codex: { tool: 'view_image', request: { path: renderedPath } },
              default: { tool: 'read_images', request: { paths: [renderedPath] } },
            },
          },
          {
            action: 'publish_after_visual_acceptance',
            tool: 'render_interactive',
            request: {
              title: basename(path, '.drawio'),
              path,
              file_type: 'drawio',
              description: 'Native draw.io diagram',
            },
          },
        ],
      });
    }
    return textResult({ status: 'failed', errors: [`unknown tool ${name}`] }, true);
  } catch (error) {
    return textResult({
      status: 'failed',
      errors: [error instanceof Error ? error.message : String(error)],
    }, true);
  }
});

const transport = new StdioServerTransport();
await server.connect(transport);

async function shutdown() {
  await upstream.close();
  await server.close();
}

process.once('SIGTERM', () => void shutdown());
process.once('SIGINT', () => void shutdown());
