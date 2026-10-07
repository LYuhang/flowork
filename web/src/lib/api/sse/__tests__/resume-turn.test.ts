import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { fetchEventSource } from '@microsoft/fetch-event-source';

import { resumeActiveTurn } from '@/lib/api/sse/resume-turn';
import { useChatStreamStore } from '@/stores/chat-stream';
import { fetchChatHistory } from '@/lib/api/queries/chats';
import { queryClient } from '@/app/query-client';
vi.mock('@/lib/api/queries/chats', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/api/queries/chats')>(),
  fetchChatHistory: vi.fn(async () => ({ items: [], total: 0, offset: 0, limit: 30 })),
}));
import {
  releaseTurnStream,
  tryAcquireTurnStream,
} from '@/lib/api/sse/turn-stream-coordinator';

describe('resumeActiveTurn HITL projection', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    useChatStreamStore.getState().reset();
    queryClient.clear();
  });

  it('waits for fixed history before exposing replayed messages', async () => {
    let resolveHistory!: (value: Awaited<ReturnType<typeof fetchChatHistory>>) => void;
    vi.mocked(fetchChatHistory).mockImplementationOnce(() => new Promise(resolve => { resolveHistory = resolve; }));
    const stream: typeof fetchEventSource = async (_url, opts) => {
      const opening = opts.onopen?.(new Response('', { status: 200 }));
      expect(useChatStreamStore.getState().runtimes.chat_order?.projectionActive).not.toBe(true);
      resolveHistory({ items: [], total: 0, offset: 0, limit: 30 });
      await opening;
      expect(useChatStreamStore.getState().runtimes.chat_order.projectionActive).toBe(true);
      opts.onmessage?.({ id: '1', event: 'done', data: JSON.stringify({ ok: true }) });
    };
    await expect(resumeActiveTurn({ wfId: 'scope_order', chatId: 'chat_order', turnId: 'turn_order' }, stream)).resolves.toBe(true);
  });

  it('shows confirmed running state before transport and history are ready', async () => {
    const stream: typeof fetchEventSource = async (_url, opts) => {
      const before = useChatStreamStore.getState().runtimes.chat_slow;
      expect(before.state).toBe('streaming');
      expect(before.projectionActive).toBe(false);
      expect(before.messages).toEqual([]);
      await opts.onopen?.(new Response('', { status: 200 }));
      opts.onmessage?.({ id: '1', event: 'done', data: JSON.stringify({ ok: true }) });
    };
    await resumeActiveTurn({ wfId: 'scope_slow', chatId: 'chat_slow', turnId: 'slow', status: 'running' }, stream);
  });

  it('restores the durable active-Turn user message before replaying events', async () => {
    const stream: typeof fetchEventSource = async (_url, opts) => {
      await opts.onopen?.(new Response('', { status: 200 }));
      const runtime = useChatStreamStore.getState().runtimes.chat_input;
      expect(fetchChatHistory).toHaveBeenCalledWith('scope_input', 'chat_input', 'turn_input');
      expect(queryClient.getQueryData(['chat-history', 'scope_input', 'chat_input', 'turn_input'])).toMatchObject({ total: 0 });
      expect(runtime.messages).toEqual([
        expect.objectContaining({
          role: 'user',
          content: 'Approve the browser click',
        }),
      ]);
      opts.onmessage?.({
        id: '1',
        event: 'done',
        data: JSON.stringify({ ok: true }),
      });
    };

    await expect(resumeActiveTurn({
      wfId: 'scope_input',
      chatId: 'chat_input',
      turnId: 'turn_input',
      inputMessage: {
        id: 'chat_input:user:turn_input',
        role: 'user',
        content: 'Approve the browser click',
        attachments: [],
      },
    }, stream)).resolves.toBe(true);
  });

  it('restores waiting-for-user state without pretending the Runtime is thinking', async () => {
    const stream: typeof fetchEventSource = async (_url, opts) => {
      await opts.onopen?.(new Response('', { status: 200 }));
      const runtime = useChatStreamStore.getState().runtimes.chat_waiting;
      expect(runtime.state).toBe('streaming');
      expect(runtime.waitingForUser).toBe(true);
      opts.onmessage?.({
        id: '1',
        event: 'done',
        data: JSON.stringify({ ok: true }),
      });
    };

    await expect(resumeActiveTurn({
      wfId: 'scope_waiting',
      chatId: 'chat_waiting',
      turnId: 'turn_waiting',
      status: 'waiting_approval',
    }, stream)).resolves.toBe(true);
  });

  it('does not overwrite later tool events with the initial pending projection', async () => {
    const stream: typeof fetchEventSource = async (_url, opts) => {
      await opts.onopen?.(new Response('', { status: 200 }));
      opts.onmessage?.({
        id: '1',
        event: 'CHAT_EVENT',
        data: JSON.stringify({
          type: 'tool_start',
          message_id: 'assistant_1',
          tool_call_id: 'call_approval',
          name: 'browser_click',
        }),
      });
      opts.onmessage?.({
        id: '2',
        event: 'CHAT_EVENT',
        data: JSON.stringify({
          type: 'tool_end',
          tool_call_id: 'call_approval',
          content: 'Clicked.',
          status: 'done',
        }),
      });
      const call = useChatStreamStore.getState().runtimes.chat_1.messages
        .flatMap((message) => message.tool_calls).find((item) => item.id === 'call_approval');
      expect(call?.status).toBe('done');
      expect(call?.result).toBe('Clicked.');
      opts.onmessage?.({
        id: '3',
        event: 'done',
        data: JSON.stringify({ ok: true }),
      });
    };

    const resumed = await resumeActiveTurn({
      wfId: 'scope_1',
      chatId: 'chat_1',
      turnId: 'turn_1',
      pendingHitl: [{
        hitlRequestId: 'hitl_1',
        hitlType: 'pre_tool_approval',
        status: 'pending',
        uiProjectionEvent: {
          type: 'tool_update',
          tool_call_id: 'call_approval',
          status: 'running',
          artifact: { meta: { pending_approval: true } },
        },
      }],
    }, stream);

    expect(resumed).toBe(true);

  });

  it('deduplicates concurrent reconciliation streams for the same run', async () => {
    let transportCalls = 0;
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const stream: typeof fetchEventSource = async (_url, opts) => {
      transportCalls += 1;
      await opts.onopen?.(new Response('', { status: 200 }));
      await held;
      opts.onmessage?.({
        id: '1',
        event: 'done',
        data: JSON.stringify({ ok: true }),
      });
    };
    const turn = {
      wfId: 'scope_dedupe',
      chatId: 'chat_dedupe',
      turnId: 'turn_dedupe',
    };

    const first = resumeActiveTurn(turn, stream);
    const second = resumeActiveTurn(turn, stream);
    await Promise.resolve();
    expect(transportCalls).toBe(1);

    release();
    await expect(Promise.all([first, second])).resolves.toEqual([true, true]);
  });

  it('does not open a replay stream while the submission stream owns the chat', async () => {
    const lease = tryAcquireTurnStream(
      'scope_submission',
      'chat_submission',
      'submission',
    );
    expect(lease).not.toBeNull();

    let replayTransportCalls = 0;
    const stream: typeof fetchEventSource = async () => {
      replayTransportCalls += 1;
    };
    const turn = {
      wfId: 'scope_submission',
      chatId: 'chat_submission',
      turnId: 'turn_submission',
    };

    await expect(resumeActiveTurn(turn, stream)).resolves.toBe(true);
    expect(replayTransportCalls).toBe(0);

    releaseTurnStream(lease!);
    const terminalStream: typeof fetchEventSource = async (_url, opts) => {
      replayTransportCalls += 1;
      await opts.onopen?.(new Response('', { status: 200 }));
      opts.onmessage?.({
        id: '1',
        event: 'done',
        data: JSON.stringify({ ok: true }),
      });
    };
    await expect(resumeActiveTurn(turn, terminalStream)).resolves.toBe(true);
    expect(replayTransportCalls).toBe(1);
  });

  it('keeps the same active Turn across a transient resume dependency failure', async () => {
    vi.spyOn(Math, 'random').mockReturnValue(0);
    let transportCalls = 0;
    const stream: typeof fetchEventSource = async (_url, opts) => {
      transportCalls += 1;
      if (transportCalls === 1) {
        await opts.onopen?.(new Response('', { status: 503 }));
        return;
      }
      await opts.onopen?.(new Response('', { status: 200 }));
      opts.onmessage?.({
        id: '1',
        event: 'done',
        data: JSON.stringify({ ok: true }),
      });
    };

    await expect(resumeActiveTurn({
      wfId: 'scope_transient',
      chatId: 'chat_transient',
      turnId: 'turn_transient',
    }, stream)).resolves.toBe(true);

    expect(transportCalls).toBe(2);
  });
});
