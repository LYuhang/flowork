/**
 * Unit tests for the `mergeChunks` reducer.
 *
 * The reducer is pure and does the heavy lifting of folding `role: 'tool'`
 * frames into the announcing assistant message. Coverage targets:
 *
 *   1. Plain user/assistant chunks → 1-to-1 MergedMessage rows.
 *   2. A tool-call announcement + matching `tool` chunk pair into a
 *      `tool_calls[i].result` with `status: 'done'`.
 *   3. Orphan tool chunks are dropped unless exactly one pending call makes
 *      fallback attachment unambiguous.
 *   4. Both wire shapes for tool-call announcements (nested
 *      `{ function: {...} }` and the flat `{ id, name, arguments }`) are
 *      handled.
 */
import { describe, expect, it } from 'vitest';
import { mergeChunks, type RawChunk } from '@/components/agent-sidebar/types';

describe('mergeChunks', () => {
  it('preserves the runtime-neutral invocation envelope across history folding', () => {
    const running = {
      schemaVersion: 1 as const,
      invocationId: 'call_1',
      runtime: { type: 'codex' },
      origin: { kind: 'custom_mcp' as const, serverName: 'github', toolName: 'search_repositories', qualifiedName: 'github__search_repositories' },
      capability: 'github',
      name: 'search_repositories',
      status: 'running' as const,
      input: { query: 'canvas' },
    };
    const completed = {
      ...running,
      status: 'success' as const,
      timing: { durationMs: 42 },
    };
    const merged = mergeChunks([
      {
        id: 'assistant_1',
        role: 'assistant',
        content: '',
        tool_calls: [{
          id: 'call_1',
          name: 'search_repositories',
          arguments: '{"query":"canvas"}',
          invocation: running,
        }],
      },
      {
        id: 'tool_1',
        role: 'tool',
        content: '{"content":[{"type":"text","text":"ok"}]}',
        tool_call_id: 'call_1',
        invocation: completed,
      },
    ]);

    expect(merged[0].tool_calls[0].invocation).toEqual(completed);
    expect(merged[0].tool_calls[0].status).toBe('done');
  });
  it('maps plain user/assistant chunks to one row each', () => {
    const chunks: RawChunk[] = [
      { role: 'user', content: 'hello' },
      { role: 'assistant', content: 'hi back' },
    ];
    const merged = mergeChunks(chunks);

    expect(merged).toHaveLength(2);
    expect(merged[0]).toMatchObject({ role: 'user', content: 'hello' });
    expect(merged[1]).toMatchObject({ role: 'assistant', content: 'hi back' });
    expect(merged[0].tool_calls).toEqual([]);
  });

  it('keeps an attachment-only user turn visible', () => {
    const merged = mergeChunks([{
      role: 'user',
      content: '',
      attachments: [{
        type: 'file',
        name: 'report.csv',
        path: '/data/attachments/report.csv',
        content_type: 'text/csv',
        size_bytes: 10,
      }],
    }]);
    expect(merged).toHaveLength(1);
    expect(merged[0].attachments?.[0]).toMatchObject({ name: 'report.csv' });
  });

  it('pairs a flat-shape tool-call with its result chunk', () => {
    const chunks: RawChunk[] = [
      {
        role: 'assistant',
        content: 'looking that up',
        tool_calls: [{ id: 'call_1', name: 'search', arguments: '{"q":"x"}' }],
      },
      { role: 'tool', content: '{"items":[]}', tool_call_id: 'call_1' },
    ];

    const merged = mergeChunks(chunks);
    expect(merged).toHaveLength(1);
    expect(merged[0].tool_calls[0]).toMatchObject({
      id: 'call_1',
      name: 'search',
      arguments: '{"q":"x"}',
      result: '{"items":[]}',
      status: 'done',
    });
  });

  it('pairs a nested-shape tool-call with its result chunk', () => {
    const chunks: RawChunk[] = [
      {
        role: 'assistant',
        content: '',
        tool_calls: [
          {
            id: 'call_2',
            type: 'function',
            function: { name: 'add', arguments: { a: 1, b: 2 } },
          },
        ],
      },
      { role: 'tool', content: '3', tool_call_id: 'call_2' },
    ];

    const merged = mergeChunks(chunks);
    expect(merged[0].tool_calls[0]).toMatchObject({
      id: 'call_2',
      name: 'add',
      arguments: '{"a":1,"b":2}',
      result: '3',
      status: 'done',
    });
  });

  it('drops orphan tool chunks (no matching id)', () => {
    const chunks: RawChunk[] = [
      { role: 'assistant', content: 'no tools here' },
      // No prior tool-call announcement with this id.
      { role: 'tool', content: 'stray', tool_call_id: 'unknown' },
      { role: 'tool', content: 'no id at all' },
    ];

    const merged = mergeChunks(chunks);
    expect(merged).toHaveLength(1);
    expect(merged[0].role).toBe('assistant');
    // No phantom 'tool' rows.
    expect(merged.find((m) => (m.role as string) === 'tool')).toBeUndefined();
  });

  it('does not guess where an unmatched tool result belongs', () => {
    const chunks: RawChunk[] = [
      {
        role: 'assistant',
        content: '',
        tool_calls: [{ id: 'call_create', name: 'create_workflow', arguments: '{"name":"Demo"}' }],
      },
      {
        role: 'tool',
        content: 'Created workflow wf_real: Demo',
        tool_call_id: '',
        artifact: { status: 'success', artifact: { handles: { workflow_id: 'wf_real' } } },
      },
    ];

    const merged = mergeChunks(chunks);
    expect(merged).toHaveLength(1);
    expect(merged[0].tool_calls[0]).toMatchObject({
      id: 'call_create',
      status: 'running',
    });
    expect(merged[0].tool_calls[0].result).toBeUndefined();
  });

  it('leaves a tool-call as running when its result has not arrived', () => {
    const chunks: RawChunk[] = [
      {
        role: 'assistant',
        content: 'thinking…',
        tool_calls: [{ id: 'call_3', name: 'search', arguments: '{}' }],
      },
    ];
    const merged = mergeChunks(chunks);
    expect(merged[0].tool_calls[0].status).toBe('running');
    expect(merged[0].tool_calls[0].result).toBeUndefined();
  });

  it('marks a cancelled tool-result artifact as an error', () => {
    const chunks: RawChunk[] = [
      {
        role: 'assistant',
        content: '',
        tool_calls: [{ id: 'call_4', name: 'bash', arguments: '{}' }],
      },
      {
        role: 'tool',
        content: 'Tool call cancelled by user.',
        tool_call_id: 'call_4',
        artifact: {
          status: 'error',
          error: { code: 'user_cancelled', message: 'Tool call cancelled by user.' },
        },
      },
    ];
    const merged = mergeChunks(chunks);
    expect(merged[0].tool_calls[0]).toMatchObject({
      id: 'call_4',
      result: 'Tool call cancelled by user.',
      status: 'error',
    });
  });

  it('restores a persisted tool error from its canonical invocation envelope', () => {
    const merged = mergeChunks([
      {
        role: 'assistant',
        content: '',
        tool_calls: [{ id: 'call_conflict', name: 'knowledge_update', arguments: '{}' }],
      },
      {
        role: 'tool',
        content: '{"error":"knowledge_version_conflict"}',
        tool_call_id: 'call_conflict',
        invocation: {
          schemaVersion: 1,
          invocationId: 'call_conflict',
          runtime: { type: 'codex' },
          origin: { kind: 'platform_mcp', toolName: 'knowledge_update' },
          capability: 'knowledge',
          name: 'knowledge_update',
          status: 'error',
          input: {},
          output: { isError: true },
          error: { message: 'knowledge_version_conflict' },
        },
      },
    ]);

    expect(merged[0].tool_calls[0]).toMatchObject({
      id: 'call_conflict',
      result: '{"error":"knowledge_version_conflict"}',
      status: 'error',
    });
  });

  it('coalesces cumulative assistant text frames into one growing row', () => {
    const chunks: RawChunk[] = [
      { role: 'user', content: 'explain' },
      { role: 'assistant', content: 'A' },
      { role: 'assistant', content: 'AB' },
      { role: 'assistant', content: 'AB' },
      { role: 'assistant', content: 'ABC' },
    ];

    const merged = mergeChunks(chunks);

    expect(merged).toHaveLength(2);
    expect(merged[1]).toMatchObject({
      role: 'assistant',
      content: 'ABC',
      tool_calls: [],
    });
  });

  it('coalesces repeated tool-call announcements with the same tool id', () => {
    const chunks: RawChunk[] = [
      {
        role: 'assistant',
        content: '',
        tool_calls: [{ id: 'call_5', name: 'bash', arguments: '{"cmd":"ls"}' }],
      },
      {
        role: 'assistant',
        content: '',
        tool_calls: [{ id: 'call_5', name: 'bash', arguments: '{"cmd":"ls"}' }],
      },
      { role: 'tool', content: 'ok', tool_call_id: 'call_5' },
    ];

    const merged = mergeChunks(chunks);

    expect(merged).toHaveLength(1);
    expect(merged[0].tool_calls).toHaveLength(1);
    expect(merged[0].tool_calls[0]).toMatchObject({
      id: 'call_5',
      result: 'ok',
      status: 'done',
    });
  });
});
