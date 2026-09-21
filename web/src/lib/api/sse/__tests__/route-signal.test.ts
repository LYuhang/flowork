/**
 * Unit tests for the SSE signal router.
 *
 * We exercise the testable seam `routeAgentSignalWith(client, …)` which
 * accepts an injected `QueryClient` so we can spy on `invalidateQueries`
 * without exercising the module singleton. The chat-stream store is the
 * other side-effect surface — we read its state after each call to
 * confirm the right mutator fired.
 *
 * Coverage targets:
 *   - `started` event allocates a turn (`chatId` + `turnId` set,
 *     `state: 'streaming'`).
 *   - `CHAT_UPDATE` appends a chunk to the buffer.
 *   - `META_SYNC` invalidates `['workflow', wfId]`.
 *   - `done` flips state + invalidates history.
 *   - `error` flips state to `failed`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { QueryClient } from '@tanstack/react-query';
import { routeAgentSignalWith } from '@/lib/api/sse/route-signal';
import { useChatStreamStore } from '@/stores/chat-stream';

const showNotice = vi.fn();
const noticePresentation = { showNotice };

function makeClient(): QueryClient & {
  invalidateQueries: ReturnType<typeof vi.fn>;
  setQueryData: ReturnType<typeof vi.fn>;
} {
  return {
    invalidateQueries: vi.fn(),
    setQueryData: vi.fn(),
  } as unknown as QueryClient & {
    invalidateQueries: ReturnType<typeof vi.fn>;
    setQueryData: ReturnType<typeof vi.fn>;
  };
}

const ctx = { wfId: 'wf_x', chatId: 'chat_y' };

describe('routeAgentSignalWith', () => {
  beforeEach(() => {
    useChatStreamStore.getState().reset();
  });

  it('started → beginTurn on the chat-stream store', () => {
    const client = makeClient();
    routeAgentSignalWith(client, 'started', { turn_id: 'turn_42' }, ctx);

    const s = useChatStreamStore.getState();
    expect(s.chatId).toBe('chat_y');
    expect(s.turnId).toBe('turn_42');
    expect(s.state).toBe('streaming');
    expect(client.invalidateQueries).not.toHaveBeenCalled();
  });

  it('CHAT_UPDATE appends a chunk to the buffer', () => {
    const client = makeClient();
    routeAgentSignalWith(
      client,
      'CHAT_UPDATE',
      { message: { role: 'assistant', content: 'hi' } },
      ctx,
    );

    const s = useChatStreamStore.getState();
    expect(s.buffer).toHaveLength(1);
    expect(s.buffer[0]).toMatchObject({ role: 'assistant', content: 'hi' });
  });

  it('upserts cumulative assistant frames by stable message id', () => {
    const client = makeClient();
    useChatStreamStore.getState().beginTurn('chat_y', 'turn_42');
    routeAgentSignalWith(client, 'CHAT_EVENT', {
      type: 'message_replace',
      message_id: 'assistant_1',
      content: 'partial',
    }, ctx);
    routeAgentSignalWith(client, 'CHAT_EVENT', {
      type: 'message_replace',
      message_id: 'assistant_1',
      content: 'partial response complete',
    }, ctx);

    expect(useChatStreamStore.getState().runtimes.chat_y.messages).toEqual([
      expect.objectContaining({
        id: 'assistant_1',
        content: 'partial response complete',
      }),
    ]);
  });

  it('concatenates incremental runtime deltas while the answer is still streaming', () => {
    const client = makeClient();
    useChatStreamStore.getState().beginTurn('chat_y', 'turn_42');
    routeAgentSignalWith(client, 'CHAT_EVENT', {
      type: 'message_start',
      message_id: 'assistant_delta',
      role: 'assistant',
    }, ctx);
    routeAgentSignalWith(client, 'CHAT_EVENT', {
      type: 'message_delta',
      message_id: 'assistant_delta',
      delta: 'hel',
    }, ctx);
    routeAgentSignalWith(client, 'CHAT_EVENT', {
      type: 'message_delta',
      message_id: 'assistant_delta',
      delta: 'lo',
    }, ctx);

    expect(useChatStreamStore.getState().runtimes.chat_y.messages).toEqual([
      expect.objectContaining({
        id: 'assistant_delta',
        content: 'hello',
      }),
    ]);
  });

  it('renders the runtime-neutral Todo snapshot without knowing the Runtime type', () => {
    const client = makeClient();
    useChatStreamStore.getState().beginTurn('chat_y', 'turn_42');

    routeAgentSignalWith(client, 'CHAT_EVENT', {
      type: 'todo_update',
      items: [
        { id: 1, text: 'Inspect files', status: 'done' },
        { id: 2, text: 'Implement change', status: 'in_progress' },
        { id: 3, text: 'Run tests', status: 'pending' },
      ],
    }, ctx);

    expect(useChatStreamStore.getState().runtimes.chat_y.todoItems).toEqual([
      { id: 1, text: 'Inspect files', status: 'done' },
      { id: 2, text: 'Implement change', status: 'in_progress' },
      { id: 3, text: 'Run tests', status: 'pending' },
    ]);
    expect(client.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['chat-state', 'wf_x', 'chat_y'],
    });
  });

  it('tracks real Runtime startup progress for cold and warm turns without creating a message', () => {
    const client = makeClient();
    useChatStreamStore.getState().beginTurn('chat_y', 'turn_42');

    routeAgentSignalWith(client, 'RUNTIME_STATUS', {
      first_turn: true,
      runtime_type: 'codex',
      phase: 'initializing_runtime',
      started_at: '2026-08-06T12:00:00Z',
      operation_id: 'turn_42',
    }, ctx);

    expect(useChatStreamStore.getState().runtimes.chat_y.startupProgress).toEqual({
      phase: 'initializing_runtime',
      startedAt: '2026-08-06T12:00:00Z',
      firstTurn: true,
      runtimeType: 'codex',
      operationId: 'turn_42',
    });
    routeAgentSignalWith(client, 'RUNTIME_STATUS', {
      first_turn: false,
      runtime_type: 'codex',
      phase: 'awaiting_first_output',
      started_at: '2026-08-06T12:00:01Z',
    }, ctx);
    expect(useChatStreamStore.getState().runtimes.chat_y.startupPhase)
      .toBe('awaiting_first_output');
    expect(useChatStreamStore.getState().runtimes.chat_y.messages).toEqual([]);
  });

  it('keeps assistant text segments in chronological order across tool calls', () => {
    const client = makeClient();
    useChatStreamStore.getState().beginTurn('chat_y', 'turn_42');
    routeAgentSignalWith(
      client,
      'CHAT_UPDATE',
      { message: { role: 'assistant', content: 'first text' } },
      ctx,
    );
    routeAgentSignalWith(
      client,
      'CHAT_UPDATE',
      {
        message: {
          role: 'assistant',
          content: '',
          tool_calls: [{ id: 'call_1', name: 'write_file', arguments: '{}' }],
        },
      },
      ctx,
    );
    routeAgentSignalWith(
      client,
      'CHAT_UPDATE',
      { message: { role: 'tool', content: 'ok', tool_call_id: 'call_1' } },
      ctx,
    );
    routeAgentSignalWith(
      client,
      'CHAT_UPDATE',
      { message: { role: 'assistant', content: 'second text' } },
      ctx,
    );

    const messages = useChatStreamStore.getState().runtimes.chat_y.messages;
    expect(messages.map((message) => message.content)).toEqual([
      'first text',
      '',
      'second text',
    ]);
    expect(messages[1].tool_calls[0]).toMatchObject({
      id: 'call_1',
      result: 'ok',
      status: 'done',
    });
  });

  it('keeps live stream state isolated by chat id', () => {
    const client = makeClient();
    const chatA = { wfId: 'wf_x', chatId: 'chat_a' };
    const chatB = { wfId: 'wf_x', chatId: 'chat_b' };

    useChatStreamStore.getState().beginTurn('chat_a', 'turn_a');
    useChatStreamStore.getState().appendChunk({ role: 'user', content: 'from a' }, 'chat_a');
    routeAgentSignalWith(
      client,
      'CHAT_UPDATE',
      { message: { role: 'assistant', content: 'still running a' } },
      chatA,
    );

    useChatStreamStore.getState().beginTurn('chat_b', 'turn_b');
    useChatStreamStore.getState().appendChunk({ role: 'user', content: 'from b' }, 'chat_b');
    routeAgentSignalWith(
      client,
      'CHAT_UPDATE',
      { message: { role: 'assistant', content: 'running b' } },
      chatB,
    );

    const runtimes = useChatStreamStore.getState().runtimes;
    expect(runtimes.chat_a.buffer.map((chunk) => chunk.content)).toEqual([
      'from a',
      'still running a',
    ]);
    expect(runtimes.chat_b.buffer.map((chunk) => chunk.content)).toEqual([
      'from b',
      'running b',
    ]);
  });

  it('HEARTBEAT leaves stream state unchanged', () => {
    const client = makeClient();
    useChatStreamStore.getState().beginTurn('chat_y', 'turn_42');
    useChatStreamStore.getState().appendChunk({ role: 'user', content: 'hello' });

    routeAgentSignalWith(client, 'HEARTBEAT', {}, ctx);

    const s = useChatStreamStore.getState();
    expect(s.chatId).toBe('chat_y');
    expect(s.turnId).toBe('turn_42');
    expect(s.state).toBe('streaming');
    expect(s.buffer).toEqual([{ role: 'user', content: 'hello' }]);
    expect(client.invalidateQueries).not.toHaveBeenCalled();
  });

  it('accepts USAGE as a telemetry frame without warning or mutating Chat', () => {
    const client = makeClient();
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    useChatStreamStore.getState().beginTurn('chat_y', 'turn_42');

    routeAgentSignalWith(client, 'USAGE', {
      model: 'gpt-test',
      prompt_tokens: 10,
      completion_tokens: 2,
    }, ctx);

    expect(useChatStreamStore.getState().state).toBe('streaming');
    expect(client.invalidateQueries).not.toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();
    warn.mockRestore();
  });

  it('accepts HITL_REQUIRED as a control-plane frame', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);

    routeAgentSignalWith(
      makeClient(),
      'HITL_REQUIRED',
      { hitl_request_id: 'hitl_1' },
      ctx,
    );

    expect(warn).not.toHaveBeenCalled();
    expect(useChatStreamStore.getState().runtimes.chat_y?.waitingForUser).toBe(true);

    routeAgentSignalWith(
      makeClient(),
      'HITL_RESOLVED',
      { hitl_request_id: 'hitl_1' },
      ctx,
    );
    expect(useChatStreamStore.getState().runtimes.chat_y?.waitingForUser).toBe(false);
    warn.mockRestore();
  });

  it('shares the waiting lifecycle for portable Runtime interactions', () => {
    const client = makeClient();
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);

    routeAgentSignalWith(
      client,
      'INTERACTION_REQUIRED',
      { hitl_request_id: 'hitl_input' },
      ctx,
    );
    expect(useChatStreamStore.getState().runtimes.chat_y?.waitingForUser).toBe(true);

    routeAgentSignalWith(
      client,
      'INTERACTION_RESOLVED',
      { hitl_request_id: 'hitl_input' },
      ctx,
    );
    expect(useChatStreamStore.getState().runtimes.chat_y?.waitingForUser).toBe(false);
    expect(client.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['chat-history', 'wf_x', 'chat_y'],
    });
    expect(warn).not.toHaveBeenCalled();
    warn.mockRestore();
  });

  it('preserves an interactive artifact when the terminal tool frame omits it', () => {
    const client = makeClient();
    useChatStreamStore.getState().beginTurn('chat_y', 'turn_42');
    routeAgentSignalWith(client, 'CHAT_EVENT', {
      type: 'tool_start',
      message_id: 'assistant_input',
      tool_call_id: 'request_input_1',
      name: 'request_user_input',
      arguments: '{}',
    }, ctx);
    routeAgentSignalWith(client, 'CHAT_EVENT', {
      type: 'tool_update',
      tool_call_id: 'request_input_1',
      content: 'waiting for input',
      artifact: { status: 'success', payload: { kind: 'interactive_artifact' } },
    }, ctx);
    expect(
      useChatStreamStore.getState().runtimes.chat_y.messages[0].tool_calls[0].result,
    ).toBe('waiting for input');
    routeAgentSignalWith(client, 'CHAT_EVENT', {
      type: 'tool_end',
      tool_call_id: 'request_input_1',
      status: 'done',
      content: 'submitted',
    }, ctx);

    const call = useChatStreamStore.getState().runtimes.chat_y.messages[0].tool_calls[0];
    expect(call.status).toBe('done');
    expect(call.result).toBe('submitted');
    expect(call.artifact).toEqual({
      status: 'success',
      payload: { kind: 'interactive_artifact' },
    });
  });

  it('META_SYNC invalidates the workflow query', () => {
    const client = makeClient();
    routeAgentSignalWith(client, 'META_SYNC', {}, ctx);

    expect(client.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['workflow', 'wf_x'],
    });
  });

  it('workflow mutation signals invalidate the payload workflow when present', () => {
    const client = makeClient();
    routeAgentSignalWith(client, 'VIBE_ACTION', { workflow_id: 'wf_real', updates: [] }, ctx);
    routeAgentSignalWith(client, 'META_SYNC', { meta: { workflow_id: 'wf_real' } }, ctx);

    expect(client.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['workflow', 'wf_real'],
    });
  });

  it('workflow context tool_end refreshes chat workspace, sandbox, VFS, and workflow', () => {
    const client = makeClient();
    routeAgentSignalWith(
      client,
      'CHAT_EVENT',
      {
        type: 'tool_end',
        tool_call_id: 'call_1',
        content: 'ok',
        artifact: {
          meta: { tool: 'create_workflow' },
          artifact: { handles: { workflow_id: 'wf_real' } },
        },
      },
      ctx,
    );

    expect(client.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['chat-workspace', 'chat_y'],
    });
    expect(client.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['general-chat-sandbox', 'chat_y'],
    });
    expect(client.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['chat-sandbox-statuses'],
    });
    expect(client.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['vfs'] });
    expect(client.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['workflow', 'wf_real'],
    });
  });

  it('file mutation tool_end refreshes VFS', () => {
    const client = makeClient();
    routeAgentSignalWith(
      client,
      'CHAT_EVENT',
      {
        type: 'tool_end',
        tool_call_id: 'call_1',
        content: 'ok',
        artifact: {
          meta: { tool: 'write_file' },
        },
      },
      ctx,
    );

    expect(client.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['vfs'] });
  });

  it.each(['shell', 'file_change', 'exec_command', 'apply_patch'])(
    'runtime-native %s tool_end refreshes VFS from invocation metadata',
    (toolName) => {
      const client = makeClient();
      routeAgentSignalWith(
        client,
        'CHAT_EVENT',
        {
          type: 'tool_end',
          tool_call_id: `call_${toolName}`,
          content: 'ok',
          status: 'done',
          invocation: {
            schemaVersion: 1,
            invocationId: `call_${toolName}`,
            runtime: { type: 'codex' },
            origin: 'runtime_native',
            capability: 'filesystem',
            name: toolName,
            status: 'success',
            input: {},
          },
        },
        ctx,
      );

      expect(client.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['vfs'] });
      expect(client.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['storage'] });
    },
  );

  it('done flips state to complete and invalidates chat history + sessions', () => {
    const client = makeClient();
    useChatStreamStore.getState().setState('streaming');
    routeAgentSignalWith(client, 'done', {}, ctx);

    expect(useChatStreamStore.getState().state).toBe('complete');
    expect(client.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['chat-history', 'wf_x', 'chat_y'],
    });
    expect(client.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['chats', 'wf_x'],
    });
  });

  it('hands transcript ownership to durable head before clearing the live projection', async () => {
    const client = makeClient();
    const durableHistory = {
      items: [
        { id: 'u1', role: 'user', content: 'hello' },
        { id: 'a1', role: 'assistant', content: 'world' },
      ],
      total: 2,
      limit: 200,
      offset: 0,
    };
    const loadDurableHistory = vi.fn().mockResolvedValue(durableHistory);
    const stream = useChatStreamStore.getState();
    stream.beginTurn('chat_y', 'turn_42');
    stream.appendChunk({ role: 'user', content: 'hello' }, 'chat_y');
    stream.applyEvent({
      type: 'message_replace',
      message_id: 'a1',
      content: 'world',
    }, 'chat_y');

    routeAgentSignalWith(client, 'done', {}, ctx, {
      showNotice,
      loadDurableHistory,
    });

    // The completed projection remains visible while canonical history loads.
    expect(useChatStreamStore.getState().runtimes.chat_y.projectionActive).toBe(true);

    await vi.waitFor(() => {
      expect(client.setQueryData).toHaveBeenCalledWith(
        ['chat-history', 'wf_x', 'chat_y', null],
        durableHistory,
      );
      const runtime = useChatStreamStore.getState().runtimes.chat_y;
      expect(runtime.projectionActive).toBe(false);
      expect(runtime.messages).toEqual([]);
      expect(runtime.buffer).toEqual([]);
    });
  });

  it('error flips state to failed', () => {
    const client = makeClient();
    useChatStreamStore.getState().setState('streaming');
    routeAgentSignalWith(client, 'error', {}, ctx);

    expect(useChatStreamStore.getState().state).toBe('failed');
  });

  it('cancelled terminal is explicit and not a disconnected stream', () => {
    const client = makeClient();
    useChatStreamStore.getState().beginTurn('chat_y', 'turn_42');
    routeAgentSignalWith(client, 'error', { code: 'cancelled' }, ctx);

    expect(useChatStreamStore.getState().state).toBe('cancelled');
    expect(client.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['chat-history', 'wf_x', 'chat_y'],
    });
    expect(client.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ['chats', 'wf_x'],
    });
  });
});

/**
 * NOTICE carries an explicit turn disposition. Rejections reset the
 * optimistic projection; warnings emitted during a real runtime turn do not.
 */
describe('routeAgentSignalWith — NOTICE (side-panel-only /browser refusal)', () => {
  beforeEach(() => {
    useChatStreamStore.getState().reset();
    showNotice.mockClear();
  });

  it('shows the message as a toast and resets the optimistic turn', () => {
    // Arrange: an optimistic turn is mid-flight (buffer + streaming state).
    useChatStreamStore.getState().beginTurn('chat_y', 't1');
    useChatStreamStore.getState().appendChunk({ role: 'user', content: '/browser go' });

    // No `code` → the backend `message` is shown verbatim (passthrough path).
    routeAgentSignalWith(
      makeClient(),
      'NOTICE',
      { level: 'info', message: '仅侧边栏可用', turn_disposition: 'cancel' },
      ctx,
      noticePresentation,
    );

    expect(showNotice).toHaveBeenCalledWith('info', '仅侧边栏可用');
    // The optimistic turn is cleared → no lingering bubble/dots.
    const s = useChatStreamStore.getState();
    expect(s.buffer).toEqual([]);
    expect(s.state).toBe('idle');
  });

  it('localizes by `code` (i18n key) when present', () => {
    routeAgentSignalWith(
      makeClient(),
      'NOTICE',
      {
        level: 'info',
        code: 'browser_sidepanel_only',
        message: 'fallback',
        turn_disposition: 'cancel',
      },
      ctx,
      noticePresentation,
    );
    // The i18n value for the key is shown, NOT the raw backend fallback message.
    expect(showNotice).toHaveBeenCalledTimes(1);
    const shown = showNotice.mock.calls[0][1];
    expect(shown).not.toBe('fallback');
    expect(String(shown)).toMatch(/side panel|侧边栏/);
  });

  it('routes level=warning/error to the matching toast variant', () => {
    routeAgentSignalWith(
      makeClient(),
      'NOTICE',
      { level: 'warning', message: 'heads up' },
      ctx,
      noticePresentation,
    );
    expect(showNotice).toHaveBeenCalledWith('warning', 'heads up');
  });

  it('preserves a running projection for a non-terminal runtime warning', () => {
    useChatStreamStore.getState().beginTurn('chat_y', 't1');
    useChatStreamStore.getState().appendChunk(
      { role: 'user', content: 'continue this turn' },
      'chat_y',
    );

    routeAgentSignalWith(
      makeClient(),
      'NOTICE',
      {
        level: 'warning',
        code: 'runtime_mcp_transport_unsupported',
        message: 'optional MCP was skipped',
        turn_disposition: 'continue',
      },
      ctx,
      noticePresentation,
    );

    const runtime = useChatStreamStore.getState().runtimes.chat_y;
    expect(runtime.state).toBe('streaming');
    expect(runtime.projectionActive).toBe(true);
    expect(runtime.messages[0]?.content).toBe('continue this turn');
  });

  it('a NOTICE with no message is a no-op', () => {
    routeAgentSignalWith(
      makeClient(),
      'NOTICE',
      { level: 'info' },
      ctx,
      noticePresentation,
    );
    expect(showNotice).not.toHaveBeenCalled();
  });
});

/**
 * Island-status relay. The router posts `ISLAND_PHASE` to
 * `window.parent` ONLY when framed (`window.parent !== window`). jsdom makes
 * the two equal by default, so we stub `window.parent` to a distinct object
 * carrying a spied `postMessage`, exercise each event, and assert the derived
 * phase. Then we confirm the unframed (main-app) case emits nothing.
 */
describe('routeAgentSignalWith — island phase relay (§14.1)', () => {
  const realParent = window.parent;

  function frameParent(): ReturnType<typeof vi.fn> {
    const postMessage = vi.fn();
    Object.defineProperty(window, 'parent', {
      value: { postMessage },
      configurable: true,
    });
    return postMessage;
  }

  beforeEach(() => {
    useChatStreamStore.getState().reset();
  });

  afterEach(() => {
    Object.defineProperty(window, 'parent', {
      value: realParent,
      configurable: true,
    });
  });

  it('started → ISLAND_PHASE thinking', () => {
    const post = frameParent();
    routeAgentSignalWith(makeClient(), 'started', { turn_id: 't1' }, ctx);
    expect(post).toHaveBeenCalledWith({ type: 'ISLAND_PHASE', kind: 'thinking' }, '*');
  });

  it('CHAT_UPDATE assistant content → ISLAND_PHASE streaming', () => {
    const post = frameParent();
    routeAgentSignalWith(
      makeClient(),
      'CHAT_UPDATE',
      { message: { role: 'assistant', content: 'hi' } },
      ctx,
    );
    expect(post).toHaveBeenCalledWith({ type: 'ISLAND_PHASE', kind: 'streaming' }, '*');
  });

  it('CHAT_UPDATE non-browser tool call → ISLAND_PHASE tool', () => {
    const post = frameParent();
    routeAgentSignalWith(
      makeClient(),
      'CHAT_UPDATE',
      {
        message: {
          role: 'assistant',
          content: '',
          tool_calls: [{ id: 'c1', name: 'show_workflow' }],
        },
      },
      ctx,
    );
    expect(post).toHaveBeenCalledWith({ type: 'ISLAND_PHASE', kind: 'tool' }, '*');
  });

  it('CHAT_UPDATE browser_ tool call → ISLAND_PHASE browser_tool with stripped tool name', () => {
    const post = frameParent();
    routeAgentSignalWith(
      makeClient(),
      'CHAT_UPDATE',
      {
        message: {
          role: 'assistant',
          content: '',
          tool_calls: [{ id: 'c1', function: { name: 'browser_navigate' } }],
        },
      },
      ctx,
    );
    expect(post).toHaveBeenCalledWith(
      { type: 'ISLAND_PHASE', kind: 'browser_tool', tool: 'navigate' },
      '*',
    );
  });

  it('done → ISLAND_PHASE ready', () => {
    const post = frameParent();
    routeAgentSignalWith(makeClient(), 'done', {}, ctx);
    expect(post).toHaveBeenCalledWith({ type: 'ISLAND_PHASE', kind: 'ready' }, '*');
  });

  it('not framed (window.parent === window) → no ISLAND_PHASE emitted', () => {
    // realParent is the default self-referential parent in jsdom.
    const spy = vi.spyOn(window.parent, 'postMessage');
    routeAgentSignalWith(makeClient(), 'started', { turn_id: 't1' }, ctx);
    routeAgentSignalWith(
      makeClient(),
      'CHAT_UPDATE',
      { message: { role: 'assistant', content: 'hi' } },
      ctx,
    );
    const islandCalls = spy.mock.calls.filter(
      ([msg]) =>
        typeof msg === 'object' &&
        msg !== null &&
        (msg as { type?: unknown }).type === 'ISLAND_PHASE',
    );
    expect(islandCalls).toHaveLength(0);
    spy.mockRestore();
  });
});
