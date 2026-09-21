/**
 * F1 unit tests for the tool-render layer:
 *   - parseEnvelope (valid / data-omitted / legacy plain string / error / missing status)
 *   - isLargeOmitted logic
 *   - rendererFor content_type dispatch (incl. fallback)
 *   - exitCodeTone (pure terminal exit-code color decision)
 *   - EnvelopeView render dispatch picks the right renderer per content_type
 */
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { server } from '@/__tests__/msw-handlers';
// Initialise i18n (side-effect) so `t(...)` interpolates rather than echoing
// the key — load-bearing for the ErrorCard "ask to fix" prefill assertions.
import '@/lib/i18n';
import {
  parseArtifactEnvelope,
  parseEnvelope,
  isLargeOmitted,
} from '@/components/agent-sidebar/tool-render/parseEnvelope';
import { EnvelopeView } from '@/components/agent-sidebar/tool-render/EnvelopeView';
import {
  TableView,
  INLINE_ROW_CAP,
} from '@/components/agent-sidebar/tool-render/TableView';
import {
  JsonTree,
} from '@/components/agent-sidebar/tool-render/JsonTree';
import {
  parseJson,
  countNodes,
  MAX_NODES,
} from '@/components/agent-sidebar/tool-render/json-tree-utils';
import {
  ErrorCard,
} from '@/components/agent-sidebar/tool-render/ErrorCard';
import {
  SubAgentCard,
} from '@/components/agent-sidebar/tool-render/SubAgentCard';
import {
  classifyError,
  exitCodeTone,
  parseTable,
  rendererFor,
  subAgentFromResult,
} from '@/components/agent-sidebar/tool-render/renderer-utils';
import { ToolCallBlock } from '@/components/agent-sidebar/ToolCallBlock';
import {
  UniversalToolResult,
} from '@/components/agent-sidebar/tool-render/UniversalToolResult';
import {
  parseStandardToolResult,
} from '@/components/agent-sidebar/tool-render/parseStandardToolResult';
import {
  InteractiveArtifactBlock,
  InteractiveArtifactPreview,
} from '@/components/agent-sidebar/tool-render/InteractiveArtifactBlock';
import { ChatRenderProvider } from '@/components/agent-sidebar/chat-render-context';
import { CHAT_RECONCILED_EVENT } from '@/lib/api/sse/chat-reconcile';
import { useChatStreamStore } from '@/stores/chat-stream';

/**
 * A QueryClient with Infinity staleTime so `useAgentPlan` resolves from the
 * seeded cache (or from `undefined` when unseeded) WITHOUT firing its network
 * queryFn — same pattern as agent-settings-tabs.test.tsx.
 */
function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity, gcTime: Infinity, enabled: true },
    },
  });
}

function QueryWrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={makeClient()}>
      <ChatRenderProvider value={{ chatId: 'chat-test', surface: 'chat' }}>
        {children}
      </ChatRenderProvider>
    </QueryClientProvider>
  );
}

function loadInteractiveSandboxDocument(frame: HTMLIFrameElement): string {
  const target = frame.contentWindow;
  expect(target).not.toBeNull();
  const postMessage = vi.spyOn(target as Window, 'postMessage');
  fireEvent.load(frame);
  const call = postMessage.mock.calls.find(([payload]) => (
    typeof payload === 'object'
    && payload !== null
    && (payload as Record<string, unknown>).channel === 'vibecanvas:interactive-loader:v1'
    && (payload as Record<string, unknown>).type === 'load'
  ));
  postMessage.mockRestore();
  expect(call).toBeDefined();
  const html = (call?.[0] as Record<string, unknown> | undefined)?.html;
  expect(typeof html).toBe('string');
  return String(html);
}

describe('parseEnvelope', () => {
  it('parses a valid success envelope with inline data', () => {
    const env = parseEnvelope(
      JSON.stringify({
        status: 'success',
        error: null,
        abstract: 'ran ls, 3 files',
        output: { content_type: 'text/shell', data: 'a\nb\nc', exit_code: 0 },
      }),
    );
    expect(env).not.toBeNull();
    expect(env?.status).toBe('success');
    expect(env?.abstract).toBe('ran ls, 3 files');
    expect(env?.output?.content_type).toBe('text/shell');
    expect(env?.output?.data).toBe('a\nb\nc');
    expect(env?.output?.exit_code).toBe(0);
  });

  it('parses a data-omitted (large) envelope', () => {
    const env = parseEnvelope(
      JSON.stringify({
        status: 'success',
        error: null,
        abstract: 'big output',
        output: { content_type: 'text/shell', path: '/exec/x.txt', full_chars: 151893, full_tokens: 37973 },
      }),
    );
    expect(env).not.toBeNull();
    expect(env?.output?.data).toBeUndefined();
    expect(env?.output?.full_tokens).toBe(37973);
  });

  it('returns null for a legacy plain (non-JSON) string', () => {
    expect(parseEnvelope('just some plain text result')).toBeNull();
  });

  it('parses an error envelope', () => {
    const env = parseEnvelope(
      JSON.stringify({ status: 'error', error: 'boom', abstract: 'it failed' }),
    );
    expect(env).not.toBeNull();
    expect(env?.status).toBe('error');
    expect(env?.error).toBe('boom');
    expect(env?.output).toBeUndefined();
  });

  it('returns null for JSON missing a recognised status', () => {
    expect(parseEnvelope(JSON.stringify({ foo: 'bar' }))).toBeNull();
    expect(parseEnvelope(JSON.stringify({ status: 'weird' }))).toBeNull();
  });

  it('returns null for undefined / empty', () => {
    expect(parseEnvelope(undefined)).toBeNull();
    expect(parseEnvelope('')).toBeNull();
  });

  it('normalizes the current product artifact without changing historical replay', () => {
    const env = parseArtifactEnvelope({
      status: 'success',
      error: null,
      content: 'stdout',
      content_abstract: 'ran command',
      artifact: {
        target: {},
        handles: { command: 'pwd', exit_code: 0, duration_ms: 12 },
      },
      payload: { size: { chars: 6, tokens: 1 } },
      meta: { content_type: 'text/shell' },
    });
    expect(env?.output).toMatchObject({
      content_type: 'text/shell',
      data: 'stdout',
      command: 'pwd',
      exit_code: 0,
      duration_ms: 12,
    });
  });
});

describe('universal MCP result fallback', () => {
  it('parses the standard MCP content and structuredContent shape', () => {
    const parsed = parseStandardToolResult(JSON.stringify({
      content: [{ type: 'text', text: '2 matches' }],
      structuredContent: { matches: [{ id: 1 }, { id: 2 }] },
      isError: false,
    }));
    expect(parsed?.content[0]).toMatchObject({ type: 'text', text: '2 matches' });
    expect(parsed?.structuredContent).toEqual({ matches: [{ id: 1 }, { id: 2 }] });
  });

  it('renders text and resource links while unknown JSON stays inspectable', () => {
    const value = parseStandardToolResult(JSON.stringify({
      content: [
        { type: 'text', text: 'ready' },
        { type: 'resource_link', name: 'Report', uri: 'https://example.test/report' },
      ],
      structuredContent: { total: 1 },
    }));
    render(<UniversalToolResult value={value!} />);
    expect(screen.getByText('ready')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Report/ })).toHaveAttribute(
      'href',
      'https://example.test/report',
    );
    expect(screen.getByText((_, element) => element?.textContent?.trim() === 'total:')).toBeInTheDocument();
  });

  it('offers Diagram Preview for JSON serialized in a standard MCP text block', () => {
    const onOpenFile = vi.fn();
    const value = parseStandardToolResult(JSON.stringify([{
      type: 'text',
      text: JSON.stringify({
        status: 'presented',
        preview_ref: {
          fileRef: { path: '/data/diagrams/system.drawio' },
        },
      }),
    }]));

    render(<UniversalToolResult value={value!} onOpenFile={onOpenFile} />);
    fireEvent.click(screen.getByRole('button', { name: 'Open diagram' }));
    expect(onOpenFile).toHaveBeenCalledWith('/data/diagrams/system.drawio');
  });

  it('returns null for plain strings so the legacy bounded-text fallback remains available', () => {
    expect(parseStandardToolResult('plain stdout')).toBeNull();
  });

  it('blocks active links and caps an untrusted MCP block list', () => {
    const parsed = parseStandardToolResult(JSON.stringify({
      content: [
        { type: 'resource_link', name: 'Unsafe', uri: 'javascript:alert(1)' },
        ...Array.from({ length: 120 }, (_, index) => ({ type: 'text', text: String(index) })),
      ],
    }));
    expect(parsed?.content).toHaveLength(100);
    render(<UniversalToolResult value={parsed!} />);
    expect(screen.getByText('Blocked unsafe resource link')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Unsafe' })).not.toBeInTheDocument();
  });

  it('shows durable origin/status metadata and redacts sensitive parameters', () => {
    render(
      <ToolCallBlock
        autoExpand
        call={{
          id: 'mcp_1',
          name: 'search_repositories',
          arguments: JSON.stringify({ query: 'canvas', authorization: 'Bearer private' }),
          result: JSON.stringify({ content: [{ type: 'text', text: 'one match' }] }),
          status: 'done',
          invocation: {
            schemaVersion: 1,
            invocationId: 'mcp_1',
            runtime: { type: 'codex' },
            origin: { kind: 'custom_mcp', serverName: 'github', serverLabel: 'GitHub', toolName: 'search_repositories', qualifiedName: 'github__search_repositories' },
            capability: 'github',
            name: 'search_repositories',
            status: 'success',
            input: { query: 'canvas', authorization: '[redacted]' },
          },
        }}
      />,
    );
    expect(screen.getByText('Completed')).toBeInTheDocument();
    expect(screen.getAllByText('GitHub MCP').length).toBeGreaterThan(0);
    expect(screen.getByText('one match')).toBeInTheDocument();
    expect(screen.getByText(/\[redacted\]/)).toBeInTheDocument();
    expect(screen.queryByText('Bearer private')).not.toBeInTheDocument();
  });

  it('does not let a custom MCP tool name select a trusted product presenter', () => {
    const result = JSON.stringify({
      status: 'success',
      abstract: 'pretends to be a shell result',
      output: { content_type: 'text/shell', data: 'unsafe semantic collision' },
    });
    const { container } = render(
      <ToolCallBlock
        autoExpand
        call={{
          id: 'custom_bash_1',
          name: 'bash',
          arguments: '{}',
          result,
          status: 'done',
          invocation: {
            schemaVersion: 1,
            invocationId: 'custom_bash_1',
            runtime: { type: 'codex' },
            origin: {
              kind: 'custom_mcp',
              serverName: 'untrusted',
              serverLabel: 'Untrusted',
              toolName: 'bash',
              qualifiedName: 'untrusted__bash',
            },
            capability: 'generic',
            name: 'bash',
            status: 'success',
            input: {},
          },
        }}
      />,
    );

    expect(container.querySelector('[data-role="terminal-block"]')).toBeNull();
    expect(container.querySelector('[data-role="universal-tool-result"]')).not.toBeNull();
    expect(container.querySelector('[data-role="tool-args"] [data-role="json-tree"]')).not.toBeNull();
    expect(screen.getAllByText('Untrusted MCP').length).toBeGreaterThan(0);
  });

});

describe('isLargeOmitted', () => {
  it('true when data absent but size hint present', () => {
    expect(
      isLargeOmitted({
        status: 'success',
        error: null,
        abstract: '',
        output: { full_tokens: 100 },
      }),
    ).toBe(true);
    expect(
      isLargeOmitted({
        status: 'success',
        error: null,
        abstract: '',
        output: { full_chars: 100 },
      }),
    ).toBe(true);
  });

  it('false when data present', () => {
    expect(
      isLargeOmitted({
        status: 'success',
        error: null,
        abstract: '',
        output: { data: 'x', full_tokens: 100 },
      }),
    ).toBe(false);
  });

  it('false when no size hint', () => {
    expect(
      isLargeOmitted({
        status: 'success',
        error: null,
        abstract: '',
        output: { path: '/x' },
      }),
    ).toBe(false);
  });

  it('false for null / no output', () => {
    expect(isLargeOmitted(null)).toBe(false);
    expect(
      isLargeOmitted({ status: 'success', error: null, abstract: '' }),
    ).toBe(false);
  });
});

describe('rendererFor', () => {
  it('text/shell → terminal', () => {
    expect(rendererFor('text/shell')).toBe('terminal');
    expect(rendererFor('text/x-diff')).toBe('diff');
    expect(rendererFor('text/diff')).toBe('diff');
  });
  it('python variants → code', () => {
    expect(rendererFor('text/python')).toBe('code');
    expect(rendererFor('text/x-python')).toBe('code');
    expect(rendererFor('application/x-python')).toBe('code');
  });
  it('text/markdown → markdown', () => {
    expect(rendererFor('text/markdown')).toBe('markdown');
  });
  it('case-insensitive', () => {
    expect(rendererFor('TEXT/SHELL')).toBe('terminal');
  });
  it('F2 content_types route to their renderers', () => {
    expect(rendererFor('application/json')).toBe('json');
    expect(rendererFor('table/jsonl')).toBe('table');
    expect(rendererFor('table/csv')).toBe('table');
    expect(rendererFor('table/tsv')).toBe('table');
    expect(rendererFor('text/html')).toBe('html');
    expect(rendererFor('link/cloud_table')).toBe('link');
    expect(rendererFor('link/anything')).toBe('link');
  });
  it('text/plain / unknown / missing → text', () => {
    expect(rendererFor('text/plain')).toBe('text');
    expect(rendererFor('something/weird')).toBe('text');
    expect(rendererFor(undefined)).toBe('text');
  });
});

describe('exitCodeTone', () => {
  it('exit 0 → green', () => {
    expect(exitCodeTone(0, 'success')).toBe('green');
  });
  it('exit non-zero → red', () => {
    expect(exitCodeTone(1, 'success')).toBe('red');
    expect(exitCodeTone(127, 'error')).toBe('red');
  });
  it('undefined exit follows status', () => {
    expect(exitCodeTone(undefined, 'success')).toBe('green');
    expect(exitCodeTone(undefined, 'error')).toBe('red');
  });
});

describe('EnvelopeView dispatch (render)', () => {
  it('text/shell renders a TerminalBlock', () => {
    render(
      <EnvelopeView
        output={{ content_type: 'text/shell', data: 'hello world', exit_code: 0 }}
        abstract="ran"
        status="success"
        wfId="wf_1"
      />,
    );
    expect(screen.getByText(/hello world/)).toBeInTheDocument();
    expect(
      document.querySelector('[data-role="terminal-block"]'),
    ).toBeInTheDocument();
  });

  it('text/python renders a CodeBlock', () => {
    render(
      <EnvelopeView
        output={{ content_type: 'text/python', data: 'print(1)' }}
        abstract="read"
        status="success"
        wfId="wf_1"
      />,
    );
    expect(
      document.querySelector('[data-role="code-block"]'),
    ).toBeInTheDocument();
  });

  it('text/markdown renders a Markdown block', () => {
    render(
      <EnvelopeView
        output={{ content_type: 'text/markdown', data: '# Title' }}
        abstract="md"
        status="success"
        wfId="wf_1"
      />,
    );
    expect(
      document.querySelector('[data-role="markdown-block"]'),
    ).toBeInTheDocument();
    expect(screen.getByText('Title')).toBeInTheDocument();
  });

  it('unknown content_type falls back to TextBlock', () => {
    render(
      <EnvelopeView
        output={{ content_type: 'something/weird', data: 'plain body' }}
        abstract="unknown"
        status="success"
        wfId="wf_1"
      />,
    );
    expect(
      document.querySelector('[data-role="text-block"]'),
    ).toBeInTheDocument();
  });

  it('status:error renders an ErrorCard (regardless of output)', () => {
    render(
      <EnvelopeView
        output={{ content_type: 'text/shell', data: 'oops' }}
        abstract="it failed"
        status="error"
        error="Connection timed out"
        wfId="wf_1"
      />,
    );
    expect(
      document.querySelector('[data-role="error-card"]'),
    ).toBeInTheDocument();
    // Did NOT render the terminal block for an error envelope.
    expect(
      document.querySelector('[data-role="terminal-block"]'),
    ).not.toBeInTheDocument();
  });

  it('table/jsonl renders a TableView', () => {
    render(
      <EnvelopeView
        output={{ content_type: 'table/jsonl', data: '{"a":1,"b":2}\n{"a":3,"b":4}' }}
        abstract="rows"
        status="success"
        wfId="wf_1"
      />,
    );
    expect(
      document.querySelector('[data-role="table-view"]'),
    ).toBeInTheDocument();
  });

  it('application/json renders a JsonTree', () => {
    render(
      <EnvelopeView
        output={{ content_type: 'application/json', data: '{"a":1}' }}
        abstract="json"
        status="success"
        wfId="wf_1"
      />,
    );
    expect(
      document.querySelector('[data-role="json-tree"]'),
    ).toBeInTheDocument();
  });

  it('text/html renders an HtmlPreview', () => {
    render(
      <EnvelopeView
        output={{ content_type: 'text/html', data: '<b>hi</b>' }}
        abstract="html"
        status="success"
        wfId="wf_1"
      />,
    );
    expect(
      document.querySelector('[data-role="html-preview"]'),
    ).toBeInTheDocument();
  });

  it('link/cloud_table renders a LinkCard', () => {
    render(
      <EnvelopeView
        output={{ content_type: 'link/cloud_table', url: 'https://example.com/t' }}
        abstract="open the sheet"
        status="success"
        wfId="wf_1"
      />,
    );
    const card = document.querySelector('[data-role="link-card"]');
    expect(card).toBeInTheDocument();
    expect(screen.getByText('open the sheet')).toBeInTheDocument();
  });

  it('no output renders the abstract', () => {
    render(
      <EnvelopeView output={undefined} abstract="just a summary" status="success" wfId={undefined} />,
    );
    expect(screen.getByText('just a summary')).toBeInTheDocument();
  });
});

describe('parseTable (TableView)', () => {
  it('parses jsonl into union-of-keys columns', () => {
    const table = parseTable(
      '{"a":1,"b":2}\n{"a":3,"c":4}',
      'table/jsonl',
    );
    expect(table).not.toBeNull();
    expect(table?.columns).toEqual(['a', 'b', 'c']);
    expect(table?.rows).toEqual([
      ['1', '2', ''],
      ['3', '', '4'],
    ]);
  });

  it('parses csv with first row as header', () => {
    const table = parseTable('name,age\nAda,36\nBob,40\n', 'table/csv');
    expect(table?.columns).toEqual(['name', 'age']);
    expect(table?.rows).toEqual([
      ['Ada', '36'],
      ['Bob', '40'],
    ]);
  });

  it('parses tsv with tab delimiter', () => {
    const table = parseTable('x\ty\n1\t2', 'table/tsv');
    expect(table?.columns).toEqual(['x', 'y']);
    expect(table?.rows).toEqual([['1', '2']]);
  });

  it('honours simple csv quoting', () => {
    const table = parseTable('a,b\n"x,y",z', 'table/csv');
    expect(table?.rows).toEqual([['x,y', 'z']]);
  });

  it('fails soft on empty / unparseable → null', () => {
    expect(parseTable('', 'table/jsonl')).toBeNull();
    expect(parseTable(undefined, 'table/jsonl')).toBeNull();
    expect(parseTable('not json at all', 'table/jsonl')).toBeNull();
  });

  it('INLINE_ROW_CAP is 20', () => {
    expect(INLINE_ROW_CAP).toBe(20);
  });

  it('caps inline render at 20 rows and shows the cap notice', () => {
    const lines = Array.from({ length: 25 }, (_, i) =>
      JSON.stringify({ i }),
    ).join('\n');
    render(
      <TableView
        output={{ content_type: 'table/jsonl', data: lines, path: '/exec/t.jsonl' }}
        abstract="big table"
        status="success"
        wfId="wf_1"
      />,
    );
    // 25 rows of data but only 20 + 1 header row rendered inline.
    const bodyRows = document.querySelectorAll(
      '[data-role="table-view"] tbody tr',
    );
    expect(bodyRows.length).toBe(20);
    expect(
      document.querySelector('[data-role="table-cap-notice"]'),
    ).toBeInTheDocument();
  });

  it('falls back to a TextBlock when the body is not a table', () => {
    render(
      <TableView
        output={{ content_type: 'table/jsonl', data: 'definitely not jsonl' }}
        abstract="x"
        status="success"
        wfId="wf_1"
      />,
    );
    expect(
      document.querySelector('[data-role="text-block"]'),
    ).toBeInTheDocument();
  });
});

describe('JsonTree', () => {
  it('parseJson succeeds on valid JSON and fails soft otherwise', () => {
    expect(parseJson('{"a":1}')).toEqual({ ok: true, value: { a: 1 } });
    expect(parseJson('nope')).toEqual({ ok: false });
    expect(parseJson(undefined)).toEqual({ ok: false });
  });

  it('countNodes counts every value node', () => {
    // { a: 1, b: [2, 3] } → root + a(1) + b(arr) + 2 + 3 = 5
    expect(countNodes({ a: 1, b: [2, 3] })).toBe(5);
    expect(countNodes(42)).toBe(1);
  });

  it('renders a tree with typed leaves; raw toggle shows pretty JSON', () => {
    render(
      <JsonTree
        output={{ content_type: 'application/json', data: '{"s":"hi","n":3,"b":true,"z":null}' }}
        abstract="json"
        status="success"
        wfId="wf_1"
      />,
    );
    expect(document.querySelector('[data-role="json-tree"]')).toBeInTheDocument();
    // Typed leaves carry data-json-kind.
    expect(document.querySelector('[data-json-kind="string"]')).toBeInTheDocument();
    expect(document.querySelector('[data-json-kind="number"]')).toBeInTheDocument();
    expect(document.querySelector('[data-json-kind="boolean"]')).toBeInTheDocument();
    expect(document.querySelector('[data-json-kind="null"]')).toBeInTheDocument();
    // Raw toggle (select by stable data-action, not translated label).
    const rawToggle = document.querySelector('[data-action="json-toggle-raw"]');
    expect(rawToggle).toBeInTheDocument();
    // This control only toggles local React state. A synchronous click keeps
    // the assertion focused on that contract and avoids user-event's pointer
    // emulation overhead when the complete suite is constrained to one worker.
    fireEvent.click(rawToggle as HTMLElement);
    expect(screen.getByText(/"s": "hi"/)).toBeInTheDocument();
  });

  it('shows a truncated notice for very large trees', () => {
    const big: Record<string, number> = {};
    for (let i = 0; i < MAX_NODES + 50; i += 1) big[`k${i}`] = i;
    render(
      <JsonTree
        output={{ content_type: 'application/json', data: JSON.stringify(big) }}
        abstract="big"
        status="success"
        wfId="wf_1"
      />,
    );
    expect(
      document.querySelector('[data-role="json-truncated-notice"]'),
    ).toBeInTheDocument();
  });

  it('falls back to TextBlock on invalid JSON', () => {
    render(
      <JsonTree
        output={{ content_type: 'application/json', data: 'not json' }}
        abstract="x"
        status="success"
        wfId="wf_1"
      />,
    );
    expect(
      document.querySelector('[data-role="text-block"]'),
    ).toBeInTheDocument();
  });
});

describe('classifyError (ErrorCard)', () => {
  it('maps common categories', () => {
    expect(classifyError('Request timed out after 30s')).toBe('timeout');
    expect(classifyError('Deadline exceeded')).toBe('timeout');
    expect(classifyError('No such file or directory')).toBe('not_found');
    expect(classifyError('File not found: /x')).toBe('not_found');
    expect(classifyError('Permission denied')).toBe('permission');
    expect(classifyError('403 Forbidden')).toBe('permission');
    expect(classifyError('Invalid argument: foo')).toBe('bad_input');
    expect(classifyError('Validation error')).toBe('bad_input');
  });
  it('unknown for unrecognised / empty', () => {
    expect(classifyError('the flux capacitor exploded')).toBe('unknown');
    expect(classifyError('')).toBe('unknown');
    expect(classifyError(null)).toBe('unknown');
    expect(classifyError(undefined)).toBe('unknown');
  });
});

describe('ErrorCard render', () => {
  it('shows a friendly category line and never the raw error for known categories', () => {
    render(<ErrorCard error="Request timed out" abstract="ran command" toolName="shell" />);
    const card = document.querySelector('[data-role="error-card"]');
    expect(card).toHaveAttribute('data-error-category', 'timeout');
    // Friendly line present; raw timeout text NOT shown as a raw line.
    expect(document.querySelector('[data-role="error-raw"]')).not.toBeInTheDocument();
  });

  it('shows the raw error in a single muted line for unknown categories', () => {
    render(<ErrorCard error="the flux capacitor exploded" abstract="" />);
    const raw = document.querySelector('[data-role="error-raw"]');
    expect(raw).toBeInTheDocument();
    expect(raw?.textContent).toContain('flux capacitor');
  });

  it('"ask to fix" prefills the composer draft in the store', async () => {
    const user = userEvent.setup();
    useChatStreamStore.getState().reset();
    render(<ErrorCard error="boom" abstract="it failed" toolName="shell" />);
    const fixBtn = document.querySelector('[data-action="error-ask-to-fix"]');
    expect(fixBtn).toBeInTheDocument();
    await user.click(fixBtn as HTMLElement);
    const draft = useChatStreamStore.getState().draft;
    expect(draft).toBeTruthy();
    expect(draft?.chatId).toBeNull();
    expect(draft?.text).toContain('shell');
    expect(draft?.text).toContain('boom');
  });
});

// Structured sub-agent presentation.

describe('subAgentFromResult', () => {
  it('parses a structured run_subagent result', () => {
    const r = subAgentFromResult(
      JSON.stringify({
        status: 'success',
        output: { answer: 'forty-two' },
        reasoning_ref: '/run/__exec__/subagents/x.jsonl',
        error: null,
      }),
    );
    expect(r?.status).toBe('success');
    expect(r?.output).toEqual({ answer: 'forty-two' });
    expect(r?.reasoning_ref).toContain('subagents');
  });

  it('fails soft on legacy / non-subagent strings', () => {
    expect(subAgentFromResult('plain text')).toBeNull();
    expect(subAgentFromResult(JSON.stringify({ status: 'success' }))).toBeNull(); // no output key
    expect(subAgentFromResult(undefined)).toBeNull();
  });
});

describe('SubAgentCard render', () => {
  it('renders the structured output (JsonTree) when expanded', async () => {
    const user = userEvent.setup();
    render(
      <SubAgentCard
        result={{ status: 'success', output: { answer: 'forty-two' }, error: null }}
        abstract="summarised the rules"
        autoExpand={false}
      />,
      { wrapper: QueryWrapper },
    );
    expect(document.querySelector('[data-role="subagent-card"]')).toBeInTheDocument();
    expect(screen.getByText('summarised the rules')).toBeInTheDocument();
    await user.click(document.querySelector('[data-action="subagent-toggle"]') as HTMLElement);
    expect(document.querySelector('[data-role="json-tree"]')).toBeInTheDocument();
  });

  it('routes an envelope-shaped output through EnvelopeView', () => {
    render(
      <SubAgentCard
        result={{
          status: 'success',
          output: { status: 'success', abstract: 'ran', output: { content_type: 'text/shell', data: 'hi' } },
          error: null,
        }}
        abstract="did a shell thing"
        autoExpand
      />,
      { wrapper: QueryWrapper },
    );
    expect(document.querySelector('[data-role="terminal-block"]')).toBeInTheDocument();
  });

  it('surfaces the worker error on a failed sub-agent', () => {
    render(
      <SubAgentCard
        result={{ status: 'error', output: null, error: 'worker blew up' }}
        abstract="tried something"
        autoExpand
      />,
      { wrapper: QueryWrapper },
    );
    expect(document.querySelector('[data-role="subagent-error"]')?.textContent).toContain(
      'worker blew up',
    );
  });
});

// ── PHASE F3: ToolCallBlock name dispatch ───────────────────────────────────

function envStr(obj: unknown): string {
  return JSON.stringify(obj);
}

describe('ToolCallBlock name dispatch', () => {
  it('run_subagent → SubAgentCard', () => {
    render(
      <ToolCallBlock
        call={{
          id: 't3',
          name: 'run_subagent',
          arguments: '{}',
          result: envStr({ status: 'success', output: { answer: 'ok' }, error: null }),
          status: 'done',
        }}
      />,
      { wrapper: QueryWrapper },
    );
    expect(document.querySelector('[data-role="subagent-card"]')).toBeInTheDocument();
  });

  it('a generic tool → EnvelopeView path (no plan/subagent surface)', () => {
    render(
      <ToolCallBlock
        call={{
          id: 't4',
          name: 'shell',
          arguments: '{}',
          result: envStr({ status: 'success', abstract: 'ran ls', output: { content_type: 'text/shell', data: 'a\nb', exit_code: 0 } }),
          status: 'done',
        }}
        autoExpand
      />,
      { wrapper: QueryWrapper },
    );
    expect(document.querySelector('[data-role="subagent-card"]')).toBeNull();
    expect(document.querySelector('[data-role="terminal-block"]')).toBeInTheDocument();
  });

  it('renders an official Playwright screenshot from its durable VFS path', async () => {
    let signedBody: Record<string, unknown> | null = null;
    server.use(
      http.post('*/api/v1/vfs/sign', async ({ request }) => {
        signedBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json({
          url: '/api/v1/vfs/raw?path=sidepanel.png&sig=test',
          expires_at: 2_000_000_000,
        });
      }),
    );
    render(
      <ToolCallBlock
        autoExpand
        vfsScopeId="chat-workspace-1"
        call={{
          id: 'browser-shot-1',
          name: 'browser_take_screenshot',
          arguments: JSON.stringify({
            filename: '/data/browser-media/sidepanel.png',
            fullPage: true,
            type: 'png',
          }),
          result: JSON.stringify({
            content: [{
              type: 'text',
              text: '### Result\n- [Screenshot of full page](browser-media/sidepanel.png)',
            }],
            structuredContent: null,
          }),
          status: 'done',
          invocation: {
            schemaVersion: 1,
            invocationId: 'browser-shot-1',
            runtime: { type: 'codex' },
            origin: {
              kind: 'platform_mcp',
              serverName: 'browser',
              serverLabel: 'browser',
              toolName: 'browser_take_screenshot',
              qualifiedName: 'browser_take_screenshot',
            },
            capability: 'browser',
            name: 'browser_take_screenshot',
            status: 'success',
            input: {},
          },
        }}
      />,
      { wrapper: QueryWrapper },
    );

    const image = await screen.findByRole('img', { name: 'browser_take_screenshot' });
    expect(image).toHaveAttribute('src', expect.stringContaining('/api/v1/vfs/raw'));
    expect(signedBody).toEqual({
      path: '/data/browser-media/sidepanel.png',
      wf_id: 'chat-workspace-1',
    });
  });
});

describe('InteractiveArtifactBlock HITL behavior', () => {
  it('renders portable Runtime input, submits it, and never echoes secret results', async () => {
    const user = userEvent.setup();
    let submittedBody: Record<string, unknown> | null = null;
    const artifact = {
      kind: 'interactive_artifact' as const,
      artifact_id: 'ia_runtime_input',
      hitl_request_id: 'hitl_runtime_input',
      title: 'Input required',
      component_type: 'user_input' as const,
      completion_mode: 'wait_for_submit' as const,
      props: {
        message: 'Choose a scope and provide the temporary token.',
        questions: [
          {
            id: 'scope',
            label: 'Scope',
            options: [
              { label: 'Current file', value: 'Current file' },
              { label: 'Workspace', value: 'Workspace' },
            ],
          },
          { id: 'token', label: 'Temporary token', secret: true, options: [] },
        ],
      },
      interaction_schema: {
        interaction_type: 'input',
        submit_label: 'Submit',
        cancel_label: 'Cancel',
        hide_result: true,
      },
      widget_state: {},
      interaction_state: { status: 'pending', is_interacted: false, result: {} },
    };
    server.use(
      http.get('*/api/v1/interactive-artifacts/ia_runtime_input', () =>
        HttpResponse.json({ artifact }),
      ),
      http.get('*/api/v1/hitl-requests/hitl_runtime_input', () =>
        HttpResponse.json({ status: 'pending', is_interacted: false }),
      ),
      http.post('*/api/v1/hitl-requests/hitl_runtime_input/decision', async ({ request }) => {
        submittedBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json({
          status: 'submitted',
          interaction_result_json: {
            artifact_id: 'ia_runtime_input',
            widget_state: { scope: 'Workspace', token: 'private-token-value' },
          },
        });
      }),
    );

    render(
      <InteractiveArtifactBlock
        call={{
          id: 'input-item-1',
          name: 'request_user_input',
          arguments: '{}',
          result: '',
          status: 'running',
          artifact: {
            status: 'success',
            payload: { artifact, pending_interaction: true },
          },
        }}
      />,
      { wrapper: QueryWrapper },
    );

    expect(await screen.findByText('Choose a scope and provide the temporary token.'))
      .toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText('Scope'), 'Workspace');
    const secretInput = screen.getByLabelText('Temporary token');
    expect(secretInput).toHaveAttribute('type', 'password');
    await user.type(secretInput, 'private-token-value');
    await user.click(screen.getByRole('button', { name: 'Submit' }));

    await waitFor(() => expect(submittedBody).not.toBeNull());
    expect(submittedBody).toMatchObject({
      decision: 'submit',
      interaction_result: {
        artifact_id: 'ia_runtime_input',
        widget_state: { scope: 'Workspace', token: 'private-token-value' },
      },
    });
    expect(await screen.findByText(/Submitted|已提交/)).toBeInTheDocument();
    expect(document.querySelector('code[title*="private-token-value"]')).toBeNull();
  });

  it('uses the same HTML runtime in the larger View surface', async () => {
    server.use(
      http.post('*/api/v1/interactive-artifacts/ia_view/resource-session', () =>
        HttpResponse.json({
          artifact_id: 'ia_view',
          resource_mounts: [
            { path_prefix: '/mount/', root_url: '/api/v1/vfs/resources/view-mount-token/' },
            { path_prefix: '/', root_url: '/api/v1/vfs/resources/view-token/' },
          ],
          base_url: '/api/v1/vfs/resources/view-token/data/',
          expires_in: 3600,
          draft_debounce_ms: 600,
        }),
      ),
      http.get('*/api/v1/interactive-artifacts/ia_view', () =>
        new HttpResponse(null, { status: 404 }),
      ),
    );
    render(
      <InteractiveArtifactPreview
        artifact={{
          kind: 'interactive_artifact',
          artifact_id: 'ia_view',
          title: 'Dataset View',
          component_type: 'html_preview',
          completion_mode: 'render_only',
          props: { html: '<p>Expanded dataset</p>' },
        }}
      />,
      { wrapper: QueryWrapper },
    );
    const frame = await waitFor(() => {
      const element = document.querySelector('[data-role="interactive-html-preview"]');
      expect(element).toBeInTheDocument();
      return element as HTMLIFrameElement;
    });
    expect(frame.style.height).toBe('662px');
    expect(frame).toHaveAttribute('src', '/interactive-sandbox.html');
    expect(loadInteractiveSandboxDocument(frame)).toContain('/api/v1/vfs/resources/view-token/');
  });

  it('renders dynamic HTML in a script-only sandbox with an ephemeral resource session', async () => {
    const onOpenFilePreview = vi.fn();
    let savedBody: Record<string, unknown> | null = null;
    let draftBody: Record<string, unknown> | null = null;
    server.use(
      http.post('*/api/v1/interactive-artifacts/ia_html/resource-session', () =>
        HttpResponse.json({
          artifact_id: 'ia_html',
          resource_mounts: [
            { path_prefix: '/mount/', root_url: '/api/v1/vfs/resources/mount-opaque/' },
            { path_prefix: '/', root_url: '/api/v1/vfs/resources/opaque/' },
          ],
          base_url: '/api/v1/vfs/resources/opaque/data/',
          expires_in: 3600,
          draft_debounce_ms: 600,
        }),
      ),
      http.put('*/api/v1/interactive-artifacts/ia_html/result-file', async ({ request }) => {
        savedBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json({
          path: '/data/annotations/result.json',
          content_type: 'application/json',
          size_bytes: 16,
          hash: 'sha256:saved',
          revision: 'sha256:saved',
        });
      }),
      http.put('*/api/v1/interactive-artifacts/ia_html/state', async ({ request }) => {
        draftBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json({
          status: 'saved',
          widget_state: (draftBody.state ?? {}) as Record<string, unknown>,
        });
      }),
    );
    render(
      <InteractiveArtifactBlock
        call={{
          id: 'tc_html',
          name: 'render_interactive',
          arguments: '{}',
          result: '',
          status: 'done',
          artifact: {
            status: 'success',
            payload: {
              artifact: {
                kind: 'interactive_artifact',
                artifact_id: 'ia_html',
                title: 'Locations',
                component_type: 'html_preview',
                completion_mode: 'render_only',
                props: {
                  html: '<script>for(let i=0;i<2;i++){const img=new Image();img.src=`/data/${i}.png`;document.body.append(img)}</script>',
                },
              },
            },
          },
        }}
        onOpenFilePreview={onOpenFilePreview}
      />,
      { wrapper: QueryWrapper },
    );

    const frame = await waitFor(() => {
      const element = document.querySelector('[data-role="interactive-html-preview"]');
      expect(element).toBeInTheDocument();
      return element as HTMLIFrameElement;
    });
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts allow-forms');
    expect(frame).toHaveAttribute('src', '/interactive-sandbox.html');
    expect(loadInteractiveSandboxDocument(frame)).toContain('/api/v1/vfs/resources/opaque/');
    expect(frame.getAttribute('sandbox')).not.toContain('allow-same-origin');
    window.dispatchEvent(new MessageEvent('message', {
      origin: 'null',
      source: frame.contentWindow,
      data: {
        channel: 'vibecanvas:interactive:v1',
        artifactId: 'ia_html',
        sessionNonce: 'nonce-html',
        type: 'ready',
        state: {},
      },
    }));
    window.dispatchEvent(new MessageEvent('message', {
      origin: 'null',
      source: frame.contentWindow,
      data: {
        channel: 'vibecanvas:interactive:v1',
        artifactId: 'ia_html',
        sessionNonce: 'nonce-html',
        type: 'draft',
        flush: true,
        state: { schema_version: 1, fields: { label: 'kept after failure' } },
      },
    }));
    await waitFor(() => {
      expect(draftBody).toEqual({
        state: {
          schema_version: 1,
          fields: { label: 'kept after failure' },
        },
      });
    });
    window.dispatchEvent(new MessageEvent('message', {
      origin: 'null',
      source: frame.contentWindow,
      data: {
        channel: 'vibecanvas:interactive:v1',
        artifactId: 'ia_html',
        sessionNonce: 'nonce-html',
        type: 'preview.open',
        path: '/data/report.pdf',
      },
    }));
    expect(onOpenFilePreview).toHaveBeenCalledWith('/data/report.pdf');
    window.dispatchEvent(new MessageEvent('message', {
      origin: 'null',
      source: frame.contentWindow,
      data: {
        channel: 'vibecanvas:interactive:v1',
        artifactId: 'ia_html',
        sessionNonce: 'nonce-html',
        type: 'vfs.write',
        requestId: 'write-html',
        path: '/data/annotations/result.json',
        method: 'PUT',
        content: '{"label":"pass"}',
        contentType: 'application/json',
      },
    }));
    await waitFor(() => {
      expect(savedBody).toEqual({
        path: '/data/annotations/result.json',
        content: '{"label":"pass"}',
        content_type: 'application/json',
      });
    });
  });

  it('trusts only the current iframe nonce after a live artifact becomes frozen', async () => {
    const onOpenFilePreview = vi.fn();
    server.use(
      http.post('*/api/v1/interactive-artifacts/ia_nonce_transition/resource-session', () =>
        HttpResponse.json({
          artifact_id: 'ia_nonce_transition',
          resource_mounts: [
            { path_prefix: '/', root_url: '/api/v1/vfs/resources/nonce-transition/' },
          ],
          base_url: '/api/v1/vfs/resources/nonce-transition/data/',
          expires_in: 3600,
          draft_debounce_ms: 600,
        }),
      ),
    );
    const callFor = (status: 'pending' | 'continued') => ({
      id: 'tc_nonce_transition',
      name: 'render_interactive',
      arguments: '{}',
      result: '',
      status: 'done' as const,
      artifact: {
        status: 'success' as const,
        payload: {
          artifact: {
            kind: 'interactive_artifact' as const,
            artifact_id: 'ia_nonce_transition',
            title: 'Nonce transition',
            component_type: 'html_preview' as const,
            completion_mode: 'render_only' as const,
            props: { html: '<button>Open</button>' },
            interaction_state: { status, is_interacted: status !== 'pending' },
          },
        },
      },
    });
    const { rerender } = render(
      <InteractiveArtifactBlock
        call={callFor('pending')}
        onOpenFilePreview={onOpenFilePreview}
      />,
      { wrapper: QueryWrapper },
    );

    const liveFrame = await waitFor(() => {
      const element = document.querySelector('[data-role="interactive-html-preview"]');
      expect(element).toBeInTheDocument();
      return element as HTMLIFrameElement;
    });
    const liveWindow = liveFrame.contentWindow;
    window.dispatchEvent(new MessageEvent('message', {
      origin: 'null',
      source: liveWindow,
      data: {
        channel: 'vibecanvas:interactive:v1',
        artifactId: 'ia_nonce_transition',
        sessionNonce: 'nonce-live',
        type: 'ready',
        state: {},
      },
    }));
    window.dispatchEvent(new MessageEvent('message', {
      origin: 'null',
      source: liveWindow,
      data: {
        channel: 'vibecanvas:interactive:v1',
        artifactId: 'ia_nonce_transition',
        sessionNonce: 'nonce-live',
        type: 'preview.open',
        path: '/data/live.pdf',
      },
    }));
    expect(onOpenFilePreview).toHaveBeenCalledWith('/data/live.pdf');

    rerender(
      <InteractiveArtifactBlock
        call={callFor('continued')}
        onOpenFilePreview={onOpenFilePreview}
      />,
    );
    const frozenFrame = await waitFor(() => {
      const element = document.querySelector('[data-role="interactive-html-preview"]') as HTMLIFrameElement;
      expect(element).not.toBe(liveFrame);
      return element;
    });
    expect(loadInteractiveSandboxDocument(frozenFrame)).toContain('&quot;frozen&quot;:true');
    const frozenWindow = frozenFrame.contentWindow;
    window.dispatchEvent(new MessageEvent('message', {
      origin: 'null',
      source: frozenWindow,
      data: {
        channel: 'vibecanvas:interactive:v1',
        artifactId: 'ia_nonce_transition',
        sessionNonce: 'nonce-frozen',
        type: 'ready',
        state: {},
      },
    }));
    window.dispatchEvent(new MessageEvent('message', {
      origin: 'null',
      source: frozenWindow,
      data: {
        channel: 'vibecanvas:interactive:v1',
        artifactId: 'ia_nonce_transition',
        sessionNonce: 'nonce-frozen',
        type: 'preview.open',
        path: '/data/frozen.pdf',
      },
    }));
    expect(onOpenFilePreview).toHaveBeenCalledWith('/data/frozen.pdf');

    for (const [source, sessionNonce, path] of [
      [frozenWindow, 'nonce-live', '/data/stale-nonce.pdf'],
      [liveWindow, 'nonce-live', '/data/stale-frame.pdf'],
    ] as const) {
      window.dispatchEvent(new MessageEvent('message', {
        origin: 'null',
        source,
        data: {
          channel: 'vibecanvas:interactive:v1',
          artifactId: 'ia_nonce_transition',
          sessionNonce,
          type: 'preview.open',
          path,
        },
      }));
    }
    expect(onOpenFilePreview).toHaveBeenCalledTimes(2);
  });

  it('shows actionable resource-session diagnostics instead of a generic render failure', async () => {
    const user = userEvent.setup();
    useChatStreamStore.getState().reset();
    server.use(
      http.post('*/api/v1/interactive-artifacts/ia_broken/resource-session', () =>
        HttpResponse.json({ detail: 'resource capability unavailable' }, { status: 503 }),
      ),
    );

    render(
      <InteractiveArtifactBlock
        call={{
          id: 'tc_broken',
          name: 'render_interactive',
          arguments: '{"component_type":"html_preview","title":"Broken preview"}',
          result: '',
          status: 'done',
          artifact: {
            status: 'success',
            payload: {
              artifact: {
                kind: 'interactive_artifact',
                artifact_id: 'ia_broken',
                title: 'Broken preview',
                component_type: 'html_preview',
                completion_mode: 'render_only',
                props: { html: '<p>content</p>' },
              },
            },
          },
        }}
      />,
      { wrapper: QueryWrapper },
    );

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('interactive artifact request failed: 503');
    expect(alert).toHaveTextContent(/Interactive preview failed|交互预览渲染失败/);

    await user.click(screen.getByRole('button', { name: /Feedback|反馈/ }));
    const feedback = useChatStreamStore.getState().draft?.text ?? '';
    expect(feedback).toContain('render_interactive');
    expect(feedback).toContain('"component_type":"html_preview"');
    expect(feedback).toContain('interactive artifact request failed: 503');
  });

  it('renders a file path through the same opaque artifact resource session', async () => {
    server.use(
      http.post('*/api/v1/interactive-artifacts/ia_image/resource-session', () =>
        HttpResponse.json({
          artifact_id: 'ia_image',
          resource_mounts: [
            { path_prefix: '/mount/', root_url: '/api/v1/vfs/resources/image-mount-token/' },
            { path_prefix: '/', root_url: '/api/v1/vfs/resources/image-token/' },
          ],
          base_url: '/api/v1/vfs/resources/image-token/data/',
          expires_in: 3600,
          draft_debounce_ms: 600,
        }),
      ),
    );
    render(
      <InteractiveArtifactBlock
        call={{
          id: 'tc_image',
          name: 'render_interactive',
          arguments: '{}',
          result: '',
          status: 'done',
          artifact: {
            status: 'success',
            payload: {
              artifact: {
                kind: 'interactive_artifact',
                artifact_id: 'ia_image',
                title: 'Screenshot',
                component_type: 'file_preview',
                completion_mode: 'render_only',
                props: { path: '/data/browser-media/screenshot.png', file_type: 'image' },
              },
            },
          },
        }}
      />,
      { wrapper: QueryWrapper },
    );

    expect(await screen.findByText('screenshot.png')).toBeInTheDocument();
    expect(screen.getByText(/^image$/i)).toBeInTheDocument();
    expect(document.querySelector('[data-preview-render-state="summary"]')).toBeInTheDocument();
  });

  it('keeps a large office artifact lightweight until the full Preview is opened', () => {
    const onOpenFilePreview = vi.fn();
    render(
      <InteractiveArtifactBlock
        call={{
          id: 'tc_lightweight_file',
          name: 'render_interactive',
          arguments: '{}',
          result: '',
          status: 'done',
          artifact: {
            status: 'success',
            payload: {
              artifact: {
                kind: 'interactive_artifact',
                artifact_id: 'ia_lightweight_file',
                title: 'Large workbook',
                component_type: 'file_preview',
                completion_mode: 'render_only',
                props: { path: '/data/large-workbook.xlsx', file_type: 'xlsx' },
              },
            },
          },
        }}
        onOpenFilePreview={onOpenFilePreview}
      />,
      { wrapper: QueryWrapper },
    );

    expect(screen.getByText('large-workbook.xlsx')).toBeInTheDocument();
    expect(screen.getByText(/^xlsx$/i)).toBeInTheDocument();
    const summary = document.querySelector('[data-preview-render-state="summary"]');
    expect(summary).toBeInTheDocument();
    expect(
      summary?.querySelector('[data-message-content-rail="assistant"]'),
    ).toHaveClass('max-w-[30rem]');
    expect(document.querySelector('[data-role="interactive-artifact-body"]')).not.toBeInTheDocument();
    expect(onOpenFilePreview).not.toHaveBeenCalled();
  });

  it('keeps the file maximize action available in a compact sidebar', async () => {
    const user = userEvent.setup();
    const onOpenFilePreview = vi.fn();
    render(
      <InteractiveArtifactBlock
        compact
        call={{
          id: 'tc_sidebar_file',
          name: 'render_interactive',
          arguments: '{}',
          result: '',
          status: 'done',
          artifact: {
            status: 'success',
            payload: {
              artifact: {
                kind: 'interactive_artifact',
                artifact_id: 'ia_sidebar_file',
                title: 'Quarterly brief',
                component_type: 'file_preview',
                completion_mode: 'render_only',
                props: { path: '/data/quarterly-brief.docx', file_type: 'docx' },
              },
            },
          },
        }}
        onOpenFilePreview={onOpenFilePreview}
      />,
      { wrapper: QueryWrapper },
    );

    await user.click(screen.getByRole('button', {
      name: 'Open in a new Preview tab',
    }));
    expect(onOpenFilePreview).toHaveBeenCalledWith('/data/quarterly-brief.docx');
  });

  it('routes an inline diagram through Preview and opens the full Preview on request', async () => {
    const user = userEvent.setup();
    const onOpenFilePreview = vi.fn();
    server.use(
      http.post('*/api/v1/previews/resolve', () => HttpResponse.json({
        schemaVersion: 1,
        fileRef: {
          schemaVersion: 1,
          scope: 'chat',
          chatId: 'chat-diagram',
          path: '/data/diagrams/system.drawio',
        },
        name: 'system.drawio',
        sizeBytes: 1024,
        contentType: 'application/vnd.jgraph.mxfile',
        detectedType: 'drawio',
        revision: 'sha256:inline-fit',
        renderer: 'drawio',
        loadPolicy: 'range',
        capabilities: { preview: true, edit: false, download: true },
        diagram: { status: 'valid', scene: null, issues: [] },
      })),
    );
    render(
      <ChatRenderProvider value={{ chatId: 'chat-diagram', surface: 'chat' }}>
        <InteractiveArtifactBlock
          call={{
            id: 'tc_diagram',
            name: 'render_interactive',
            arguments: '{}',
            result: '',
            status: 'done',
            artifact: {
              status: 'success',
              payload: {
                artifact: {
                  kind: 'interactive_artifact',
                  artifact_id: 'ia_diagram',
                  title: 'System overview',
                  component_type: 'file_preview',
                  completion_mode: 'render_only',
                  props: {
                    path: '/data/diagrams/system.drawio',
                    file_type: 'drawio',
                  },
                },
              },
            },
          }}
          onOpenFilePreview={onOpenFilePreview}
        />
      </ChatRenderProvider>,
      { wrapper: QueryWrapper },
    );

    expect(await screen.findByText('system.drawio')).toBeInTheDocument();
    expect(screen.getByText(/^drawio$/i)).toBeInTheDocument();
    expect(onOpenFilePreview).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Open in preview' }));
    expect(onOpenFilePreview).toHaveBeenCalledWith('/data/diagrams/system.drawio');
  });

  it('keeps a draw.io artifact lightweight when persisted metadata contains an older MIME hint', async () => {
    const user = userEvent.setup();
    const onOpenFilePreview = vi.fn();
    render(
      <InteractiveArtifactBlock
        call={{
          id: 'tc_diagram_with_mime',
          name: 'render_interactive',
          arguments: '{}',
          result: '',
          status: 'done',
          artifact: {
            status: 'success',
            payload: {
              artifact: {
                kind: 'interactive_artifact',
                artifact_id: 'ia_diagram_with_mime',
                title: 'Product launch readiness',
                component_type: 'file_preview',
                completion_mode: 'render_only',
                props: {
                  path: '/data/diagrams/product-launch-readiness.drawio',
                  mime: 'application/vnd.jgraph.mxfile',
                  description: 'Editable native draw.io file',
                },
              },
            },
          },
        }}
        onOpenFilePreview={onOpenFilePreview}
      />,
      { wrapper: QueryWrapper },
    );

    expect(screen.getByText('product-launch-readiness.drawio')).toBeInTheDocument();
    expect(screen.getByText(/^drawio$/i)).toBeInTheDocument();
    expect(screen.queryByText('Interactive preview failed')).not.toBeInTheDocument();
    expect(document.querySelector('[data-preview-render-state="summary"]')).toBeInTheDocument();
    expect(document.querySelector('[data-role="interactive-artifact-body"]')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Open in preview' }));
    expect(onOpenFilePreview).toHaveBeenCalledWith(
      '/data/diagrams/product-launch-readiness.drawio',
    );
  });

  it('renders a URL artifact as an isolated interactive WebView', async () => {
    render(
      <InteractiveArtifactBlock
        call={{
          id: 'tc_url',
          name: 'render_url_preview',
          arguments: '{}',
          result: '',
          status: 'done',
          artifact: {
            status: 'success',
            payload: {
              artifact: {
                kind: 'interactive_artifact',
                artifact_id: 'ia_url',
                title: 'Reference page',
                component_type: 'url_preview',
                completion_mode: 'render_only',
                props: {
                  url: 'https://example.com/docs',
                  description: 'External documentation',
                },
              },
            },
          },
        }}
      />,
      { wrapper: QueryWrapper },
    );

    const frame = await screen.findByTitle('Reference page');
    expect(frame).toHaveAttribute('src', 'https://example.com/docs');
    expect(frame).toHaveAttribute('sandbox', expect.stringContaining('allow-scripts'));
    expect(screen.getByText('External documentation')).toBeInTheDocument();
    expect(screen.getByText('Web preview')).toBeInTheDocument();
    expect(document.querySelector('[data-tool-name="render_url_preview"]')).toBeInTheDocument();
  });

  it('loads an HTML file path and renders it through the isolated HTML runtime', async () => {
    let sessionReads = 0;
    server.use(
      http.post('*/api/v1/interactive-artifacts/ia_html_file/resource-session', () => {
        sessionReads += 1;
        return HttpResponse.json({
          artifact_id: 'ia_html_file',
          resource_mounts: [
            {
              path_prefix: '/mount/',
              root_url: '/api/v1/vfs/resources/html-mount-token/mount/',
            },
            { path_prefix: '/', root_url: '/api/v1/vfs/resources/html-token/' },
          ],
          base_url: '/api/v1/vfs/resources/html-token/data/',
          expires_in: 3600,
          draft_debounce_ms: 600,
        });
      }),
      http.get('*/api/v1/vfs/resources/html-mount-token/mount/data/view.html', () =>
        HttpResponse.text('<h1 id="dataset-title">Dataset preview</h1><img src="https://media.example.test/frame.png">'),
      ),
    );
    render(
      <InteractiveArtifactBlock
        call={{
          id: 'tc_html_file',
          name: 'render_interactive',
          arguments: '{}',
          result: '',
          status: 'done',
          artifact: {
            status: 'success',
            payload: {
              artifact: {
                kind: 'interactive_artifact',
                artifact_id: 'ia_html_file',
                title: 'Dataset HTML',
                component_type: 'file_preview',
                completion_mode: 'render_only',
                props: { path: '/mount/data/view.html' },
              },
            },
          },
        }}
      />,
      { wrapper: QueryWrapper },
    );

    expect(await screen.findByText('view.html')).toBeInTheDocument();
    expect(screen.getByText(/^html$/i)).toBeInTheDocument();
    expect(sessionReads).toBe(0);
  });

  it('uses a stable summary instead of framing a PDF blocked by application headers', async () => {
    server.use(
      http.post('*/api/v1/interactive-artifacts/ia_pdf_file/resource-session', () =>
        HttpResponse.json({
          artifact_id: 'ia_pdf_file',
          resource_mounts: [
            { path_prefix: '/', root_url: '/api/v1/vfs/resources/pdf-token/' },
          ],
          base_url: '/api/v1/vfs/resources/pdf-token/data/',
          expires_in: 3600,
          draft_debounce_ms: 600,
        }),
      ),
    );

    render(
      <InteractiveArtifactBlock
        call={{
          id: 'tc_pdf_file',
          name: 'render_interactive',
          arguments: '{}',
          result: '',
          status: 'done',
          artifact: {
            status: 'success',
            payload: {
              artifact: {
                kind: 'interactive_artifact',
                artifact_id: 'ia_pdf_file',
                title: 'Release report',
                component_type: 'file_preview',
                completion_mode: 'render_only',
                props: { path: '/data/report.pdf' },
              },
            },
          },
        }}
      />,
      { wrapper: QueryWrapper },
    );

    expect(await screen.findByText('report.pdf')).toBeInTheDocument();
    expect(screen.getByText(/^pdf$/i)).toBeInTheDocument();
  });

  it('retries an HTML file that reaches VFS after the artifact card', async () => {
    let reads = 0;
    server.use(
      http.post('*/api/v1/interactive-artifacts/ia_late_html/resource-session', () =>
        HttpResponse.json({
          artifact_id: 'ia_late_html',
          resource_mounts: [
            { path_prefix: '/', root_url: '/api/v1/vfs/resources/late-token/' },
          ],
          base_url: '/api/v1/vfs/resources/late-token/data/',
          expires_in: 3600,
          draft_debounce_ms: 600,
        }),
      ),
      http.get('*/api/v1/vfs/resources/late-token/data/late.html', () => {
        reads += 1;
        return reads === 1
          ? HttpResponse.text('not written yet', { status: 404 })
          : HttpResponse.text('<h1>Available after writeback</h1>');
      }),
    );

    render(
      <InteractiveArtifactBlock
        call={{
          id: 'tc_late_html',
          name: 'render_interactive',
          arguments: '{}',
          result: '',
          status: 'done',
          artifact: {
            status: 'success',
            payload: {
              artifact: {
                kind: 'interactive_artifact',
                artifact_id: 'ia_late_html',
                title: 'Late HTML',
                component_type: 'file_preview',
                completion_mode: 'render_only',
                props: { path: '/data/late.html' },
              },
            },
          },
        }}
      />,
      { wrapper: QueryWrapper },
    );

    expect(await screen.findByText('late.html')).toBeInTheDocument();
    expect(screen.getByText(/^html$/i)).toBeInTheDocument();
    expect(reads).toBe(0);
  });

  it('fails closed when a waiting card has no durable HITL request id', async () => {
    const user = userEvent.setup();
    const onSubmitAsNewMessage = vi.fn();
    server.use(
      http.post('*/api/v1/interactive-artifacts/ia_missing_hitl/resource-session', () =>
        HttpResponse.json({
          artifact_id: 'ia_missing_hitl',
          resource_mounts: [
            { path_prefix: '/mount/', root_url: '/api/v1/vfs/resources/mount-opaque/' },
            { path_prefix: '/', root_url: '/api/v1/vfs/resources/opaque/' },
          ],
          base_url: '/api/v1/vfs/resources/opaque/data/',
          expires_in: 3600,
          draft_debounce_ms: 600,
        }),
      ),
    );
    render(
      <InteractiveArtifactBlock
        call={{
          id: 'tc_missing_hitl',
          name: 'render_interactive',
          arguments: '{}',
          result: '',
          status: 'done',
          artifact: {
            status: 'success',
            payload: {
              artifact: {
                kind: 'interactive_artifact',
                artifact_id: 'ia_missing_hitl',
                title: 'Choose',
                component_type: 'html_preview',
                completion_mode: 'wait_for_submit',
                props: { html: '<form><input name="value"><button type="submit">Submit</button></form>' },
                interaction_state: { status: 'pending', is_interacted: false },
              },
            },
          },
        }}
        onSubmitAsNewMessage={onSubmitAsNewMessage}
      />,
      { wrapper: QueryWrapper },
    );

    await user.click(screen.getByRole('button', { name: /Submit|提交/ }));
    expect(onSubmitAsNewMessage).not.toHaveBeenCalled();
    expect(screen.getByText(/Could not save|无法保存/)).toBeInTheDocument();
  });

  it('keeps completed render-only cards stable after chat reconciliation', async () => {
    let artifactReads = 0;
    server.use(
      http.get('*/api/v1/interactive-artifacts/ia_reconcile', () => {
        artifactReads += 1;
        return HttpResponse.json({
          artifact: {
            kind: 'interactive_artifact',
            artifact_id: 'ia_reconcile',
            title: 'Reconcile me',
            component_type: 'html_preview',
            completion_mode: 'render_only',
            props: { html: '<p>Value</p>' },
            widget_state: {},
            interaction_state: { status: 'none', is_interacted: false },
          },
        });
      }),
    );

    render(
      <InteractiveArtifactBlock
        call={{
          id: 'tc_reconcile',
          name: 'render_interactive',
          arguments: '{}',
          result: '',
          status: 'done',
          artifact: {
            status: 'success',
            payload: {
              artifact: {
                kind: 'interactive_artifact',
                artifact_id: 'ia_reconcile',
                title: 'Reconcile me',
                component_type: 'html_preview',
                completion_mode: 'render_only',
                props: { html: '<p>Value</p>' },
              },
            },
          },
        }}
      />,
      { wrapper: QueryWrapper },
    );

    await waitFor(() => expect(artifactReads).toBe(1));
    window.dispatchEvent(new CustomEvent(CHAT_RECONCILED_EVENT));
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(artifactReads).toBe(1);
    expect(document.querySelectorAll('[data-action="interactive-open-artifact-preview"]')).toHaveLength(0);
  });

  it('rehydrates pending interactive cards after chat reconciliation', async () => {
    let artifactReads = 0;
    server.use(
      http.get('*/api/v1/interactive-artifacts/ia_pending_reconcile', () => {
        artifactReads += 1;
        return HttpResponse.json({
          artifact: {
            kind: 'interactive_artifact',
            artifact_id: 'ia_pending_reconcile',
            title: 'Pending interaction',
            component_type: 'user_input',
            completion_mode: 'wait_for_submit',
            props: { questions: [] },
            widget_state: {},
            interaction_state: { status: 'pending', is_interacted: false },
          },
        });
      }),
    );

    render(
      <InteractiveArtifactBlock
        call={{
          id: 'tc_pending_reconcile',
          name: 'render_interactive',
          arguments: '{}',
          result: '',
          status: 'done',
          artifact: {
            status: 'success',
            payload: {
              artifact: {
                kind: 'interactive_artifact',
                artifact_id: 'ia_pending_reconcile',
                title: 'Pending interaction',
                component_type: 'user_input',
                completion_mode: 'wait_for_submit',
                props: { questions: [] },
                interaction_state: { status: 'pending', is_interacted: false },
              },
            },
          },
        }}
      />,
      { wrapper: QueryWrapper },
    );

    await waitFor(() => expect(artifactReads).toBe(1));
    window.dispatchEvent(new CustomEvent(CHAT_RECONCILED_EVENT));
    await waitFor(() => expect(artifactReads).toBe(2));
  });

  it('renders a Continue-only gate and starts a hidden control turn', async () => {
    const user = userEvent.setup();
    let finishContinue!: () => void;
    const onSubmitAsNewMessage = vi.fn(() => new Promise<void>((resolve) => {
      finishContinue = resolve;
    }));
    let decisionPosts = 0;
    server.use(
      http.get('*/api/v1/interactive-artifacts/ia_continue', () =>
        new HttpResponse(null, { status: 404 }),
      ),
      http.get('*/api/v1/hitl-requests/hitl_continue', () =>
        HttpResponse.json({
          status: 'pending',
          interaction_result_json: {},
          is_interacted: false,
        }),
      ),
      http.post('*/api/v1/hitl-requests/hitl_continue/decision', () => {
        decisionPosts += 1;
        return HttpResponse.json({
          status: 'submitted',
          decision_applied: true,
          interaction_result_json: { decision: 'submit' },
        });
      }),
      http.post('*/api/v1/interactive-artifacts/ia_continue/resource-session', () =>
        HttpResponse.json({
          artifact_id: 'ia_continue',
          resource_mounts: [
            { path_prefix: '/mount/', root_url: '/api/v1/vfs/resources/mount-opaque/' },
            { path_prefix: '/', root_url: '/api/v1/vfs/resources/opaque/' },
          ],
          base_url: '/api/v1/vfs/resources/opaque/data/',
          expires_in: 3600,
          draft_debounce_ms: 600,
        }),
      ),
    );

    render(
      <InteractiveArtifactBlock
        call={{
          id: 'tc_continue',
          name: 'render_interactive',
          arguments: '{"require_human_confirm":true}',
          result: '',
          status: 'done',
          artifact: {
            status: 'success',
            payload: {
              artifact: {
                kind: 'interactive_artifact',
                artifact_id: 'ia_continue',
                hitl_request_id: 'hitl_continue',
                title: 'Review result',
                component_type: 'html_preview',
                completion_mode: 'wait_for_submit',
                interaction_schema: {
                  interaction_type: 'continue',
                  submit_label: 'Continue',
                },
                props: { html: '<p>Ready</p>' },
                interaction_state: { status: 'pending', is_interacted: false },
              },
            },
          },
        }}
        onSubmitAsNewMessage={onSubmitAsNewMessage}
      />,
      { wrapper: QueryWrapper },
    );

    expect(screen.queryByRole('button', { name: /Cancel|取消/ })).not.toBeInTheDocument();
    await user.click(await screen.findByRole('button', { name: /Continue/ }));
    expect(onSubmitAsNewMessage).toHaveBeenCalledWith('', {
      type: 'hitl_continue',
      version: 1,
      hitl_request_id: 'hitl_continue',
      artifact_id: 'ia_continue',
      action: 'continue',
    });
    expect(decisionPosts).toBe(0);
    expect(screen.queryByText(/Continued/)).not.toBeInTheDocument();
    finishContinue();
    const continued = await screen.findByRole('button', { name: /Continued/ });
    expect(continued).toBeDisabled();
    expect(continued).toHaveAttribute('data-state', 'continued');
    await user.click(continued);
    expect(onSubmitAsNewMessage).toHaveBeenCalledTimes(1);
  });

  it('does not start a duplicate follow-up turn when another page resolved the card', async () => {
    const user = userEvent.setup();
    const onSubmitAsNewMessage = vi.fn();
    server.use(
      http.get('*/api/v1/interactive-artifacts/ia_duplicate', () =>
        new HttpResponse(null, { status: 404 }),
      ),
      http.get('*/api/v1/hitl-requests/hitl_duplicate', () =>
        HttpResponse.json({ status: 'pending', interaction_result_json: {}, is_interacted: false }),
      ),
      http.post('*/api/v1/hitl-requests/hitl_duplicate/decision', () =>
        HttpResponse.json({
          status: 'submitted',
          decision_applied: false,
          interaction_result_json: { widget_state: { value: 3 } },
        }),
      ),
      http.post('*/api/v1/interactive-artifacts/ia_duplicate/resource-session', () =>
        HttpResponse.json({
          artifact_id: 'ia_duplicate',
          resource_mounts: [
            { path_prefix: '/mount/', root_url: '/api/v1/vfs/resources/mount-opaque/' },
            { path_prefix: '/', root_url: '/api/v1/vfs/resources/opaque/' },
          ],
          base_url: '/api/v1/vfs/resources/opaque/data/',
          expires_in: 3600,
          draft_debounce_ms: 600,
        }),
      ),
      http.put('*/api/v1/interactive-artifacts/ia_duplicate/result-file', () =>
        HttpResponse.json({
          path: '/data/interactive/ia_duplicate/result.json',
          content_type: 'application/json',
          size_bytes: 20,
          hash: 'sha256:test',
        }),
      ),
    );

    render(
      <InteractiveArtifactBlock
        call={{
          id: 'tc_duplicate',
          name: 'render_interactive',
          arguments: '{}',
          result: '',
          status: 'done',
          artifact: {
            status: 'success',
            payload: {
              artifact: {
                kind: 'interactive_artifact',
                artifact_id: 'ia_duplicate',
                hitl_request_id: 'hitl_duplicate',
                title: 'Choose a value',
                component_type: 'html_preview',
                completion_mode: 'wait_for_submit',
                props: { html: '<form><input name="value" value="3"><button type="submit">Submit</button></form>' },
                interaction_state: { status: 'pending', is_interacted: false },
              },
            },
          },
        }}
        onSubmitAsNewMessage={onSubmitAsNewMessage}
      />,
      { wrapper: QueryWrapper },
    );

    await user.click(await screen.findByRole('button', { name: /Submit|提交/ }));
    expect(await screen.findByText(/Submitted|已提交/)).toBeInTheDocument();
    expect(onSubmitAsNewMessage).not.toHaveBeenCalled();
  });

  it('does not submit a new user message for pre-tool approval cards', async () => {
    const user = userEvent.setup();
    const onSubmitAsNewMessage = vi.fn();
    let postedDecision = '';
    server.use(
      http.get('*/api/v1/interactive-artifacts/ia_approval', () =>
        new HttpResponse(null, { status: 404 }),
      ),
      http.get('*/api/v1/hitl-requests/hitl_approval', () =>
        HttpResponse.json({
          hitl_request_id: 'hitl_approval',
          chat_id: 'c1',
          run_id: 'turn_1',
          artifact_id: 'ia_approval',
          status: 'pending',
          hitl_type: 'pre_tool_approval',
          title: 'Approve browser_network_request',
          prompt_text: 'The agent wants to save a private PDF.',
          ui_payload_json: {},
          ui_projection_event_json: {},
          decision_payload_json: {},
          interaction_result_json: {},
          is_interacted: false,
          created_at: '2026-07-18T00:00:00Z',
          resolved_at: null,
        }),
      ),
      http.post('*/api/v1/hitl-requests/hitl_approval/decision', async ({ request }) => {
        const body = await request.json() as { decision?: string };
        postedDecision = body.decision ?? '';
        return HttpResponse.json({
          hitl_request_id: 'hitl_approval',
          chat_id: 'c1',
          run_id: 'turn_1',
          artifact_id: 'ia_approval',
          status: 'approved',
          hitl_type: 'pre_tool_approval',
          title: 'Approve browser_network_request',
          prompt_text: 'The agent wants to save a private PDF.',
          ui_payload_json: {},
          ui_projection_event_json: {},
          decision_payload_json: { decision: 'approve' },
          interaction_result_json: { decision: 'approve' },
          is_interacted: true,
          created_at: '2026-07-18T00:00:00Z',
          resolved_at: '2026-07-18T00:00:01Z',
        });
      }),
    );

    render(
      <InteractiveArtifactBlock
        call={{
          id: 'tc_fetch',
          name: 'browser_network_request',
          arguments: '{"url":"https://example.test/private.pdf"}',
          result: '',
          status: 'running',
          artifact: {
            status: 'success',
            payload: {
              pending_approval: true,
              artifact: {
                kind: 'interactive_artifact',
                artifact_id: 'ia_approval',
                hitl_request_id: 'hitl_approval',
                title: 'Approve browser_network_request',
                component_type: 'approval',
                completion_mode: 'wait_for_submit',
                props: {
                  fields: [
                    { name: 'tool', label: 'Tool', value: 'browser_network_request' },
                  ],
                },
                interaction_schema: {
                  submit_label: 'Approve',
                  cancel_label: 'Deny',
                },
                interaction_state: { status: 'pending', is_interacted: false },
              },
            },
            meta: {
              hitl_type: 'pre_tool_approval',
              pending_approval: true,
            },
          },
        }}
        onSubmitAsNewMessage={onSubmitAsNewMessage}
      />,
      { wrapper: QueryWrapper },
    );

    await user.click(await screen.findByRole('button', { name: /Approve|允许/ }));

    expect(postedDecision).toBe('approve');
    expect(onSubmitAsNewMessage).not.toHaveBeenCalled();
    expect(await screen.findByText(/Approved|已允许/)).toBeInTheDocument();
  });
});
