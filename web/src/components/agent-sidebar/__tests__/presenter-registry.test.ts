import { describe, expect, it } from 'vitest';

import type { MergedToolCall } from '@/components/agent-sidebar/types';
import { selectToolPresenter } from '@/components/agent-sidebar/tool-render/presenterRegistry';

const envelope = {
  status: 'success' as const,
  error: null,
  abstract: 'done',
  output: { content_type: 'text/plain', data: 'output' },
};

function call(overrides: Partial<MergedToolCall>): MergedToolCall {
  return {
    id: 'call-1',
    name: 'unknown',
    arguments: '{}',
    result: 'result',
    status: 'done',
    ...overrides,
  };
}

describe('tool presenter registry', () => {
  it('selects terminal and diff from trusted cross-runtime capabilities', () => {
    expect(selectToolPresenter({
      call: call({
        name: 'exec',
        invocation: {
          schemaVersion: 1,
          invocationId: 'i1',
          runtime: { type: 'codex' },
          origin: { kind: 'runtime_native' },
          capability: 'shell.execute',
          name: 'exec',
          status: 'success',
          input: {},
        },
      }),
      envelope,
      hasUniversal: false,
    })).toBe('terminal');

    expect(selectToolPresenter({
      call: call({ name: 'edit_file' }),
      envelope,
      hasUniversal: false,
    })).toBe('diff');
  });

  it('does not let a custom MCP collide with trusted terminal presenters', () => {
    expect(selectToolPresenter({
      call: call({
        name: 'bash',
        invocation: {
          schemaVersion: 1,
          invocationId: 'i2',
          runtime: { type: 'codex' },
          origin: { kind: 'custom_mcp', serverName: 'untrusted' },
          capability: 'shell.execute',
          name: 'bash',
          status: 'success',
          input: {},
          presentation: { kind: 'terminal' },
        },
      }),
      envelope: null,
      hasUniversal: true,
    })).toBe('universal');
  });

  it('falls back for unknown tools without losing their output', () => {
    expect(selectToolPresenter({
      call: call({}),
      envelope: null,
      hasUniversal: true,
    })).toBe('universal');
    expect(selectToolPresenter({
      call: call({}),
      envelope: null,
      hasUniversal: false,
    })).toBe('plain-text');
  });

  it('uses the browser presenter for official Playwright standard MCP output', () => {
    expect(selectToolPresenter({
      call: call({
        name: 'browser_take_screenshot',
        invocation: {
          schemaVersion: 1,
          invocationId: 'browser-1',
          runtime: { type: 'codex' },
          origin: { kind: 'platform_mcp', serverName: 'browser' },
          capability: 'browser',
          name: 'browser_take_screenshot',
          status: 'success',
          input: {},
        },
      }),
      envelope: null,
      hasUniversal: true,
    })).toBe('browser');
  });
});
