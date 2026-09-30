import { beforeEach, describe, expect, it, vi } from 'vitest';
import { http, HttpResponse } from 'msw';
import { server } from '@/__tests__/msw-handlers';

let capturedBody: Record<string, unknown> | null = null;

import { streamAgentTurn } from '@/lib/api/sse/agent-stream';
import { runAgentTurn } from '@/lib/api/sse/run-agent-turn';
import { useChatStreamStore } from '@/stores/chat-stream';

describe('Agent Turn wire protocol', () => {
  beforeEach(() => {
    capturedBody = null;
    localStorage.clear();
    useChatStreamStore.getState().reset();
    server.use(
      http.post('*/api/v1/chat-scopes/:scopeId/chats/:chatId/messages', async ({ request }) => {
        capturedBody = await request.json() as Record<string, unknown>;
        return new HttpResponse('id: 1\nevent: done\ndata: {}\n\n', {
          status: 200,
          headers: {
            'Content-Type': 'text/event-stream',
            'X-Turn-Id': 'turn_protocol_1',
          },
        });
      }),
    );
  });

  it('preserves the selected Skill identity in the wire body and retry snapshot', async () => {
    const skillUse = { skill_id: '0d40d11d-942a-4fb2-a2b4-15acfe9e5162', name: 'research' };
    await runAgentTurn({ wfId: 'scope_1', chatId: 'chat_skill', content: '/skill-use:[research] summarize', skillUse });
    expect(capturedBody).toMatchObject({ skill_use: skillUse });
    expect(useChatStreamStore.getState().runtimes.chat_skill.lastInput?.skillUse).toEqual(skillUse);
  });

  it.each(['selected_skill_unavailable', 'selected_skill_changed'])('explains %s without losing the rejection', async (code) => {
    server.use(
      http.post('*/api/v1/chat-scopes/:scopeId/chats/:chatId/messages', () => HttpResponse.json({ detail: { code } }, {status:409})),
      http.get('*/api/v1/chats/:chatId/turns/by-client-request/:requestId', () => HttpResponse.json({detail:'not found'},{status:404})),
    );
    await expect(streamAgentTurn({wfId:'scope_1',chatId:'chat_skill_denied',content:'/skill-use:[research] summarize',
      skillUse:{skill_id:'0d40d11d-942a-4fb2-a2b4-15acfe9e5162',name:'research'},signal:new AbortController().signal,
    })).rejects.toThrow(/Select.*again/);
  });

  it('preserves the selected approval mode and sends surface but no browser topology', async () => {
    await streamAgentTurn({
      wfId: 'scope_1',
      chatId: 'chat_1',
      content: 'read the current page',
      mode: 'browser',
      approvalMode: 'always_ask',
      surface: 'sidepanel',
      agentSurface: 'browser',
      signal: new AbortController().signal,
    });

    expect(capturedBody).toMatchObject({
      role: 'user',
      content: 'read the current page',
      mode: 'browser',
      approval_mode: 'always_ask',
      surface: 'sidepanel',
      agent_surface: 'browser',
    });
    expect(capturedBody).not.toHaveProperty('browser');
    expect(capturedBody).not.toHaveProperty('browser_id');
    expect(capturedBody).not.toHaveProperty('browser_window_id');
    expect(capturedBody).not.toHaveProperty('browser_panel_context_id');
    expect(capturedBody).not.toHaveProperty('client_context_id');
  });

  it('uses the durable HITL id as the Continue Turn idempotency key', async () => {
    const onAccepted = vi.fn();
    await streamAgentTurn({
      wfId: 'scope_1',
      chatId: 'chat_1',
      content: '',
      control: {
        type: 'hitl_continue',
        version: 1,
        hitl_request_id: 'hitl_review_1',
        artifact_id: 'ia_review_1',
        action: 'continue',
      },
      onAccepted,
      signal: new AbortController().signal,
    });

    expect(capturedBody).toMatchObject({
      role: 'user',
      content: '',
      client_request_id: 'hitl_continue:hitl_review_1',
      control: {
        type: 'hitl_continue',
        version: 1,
        hitl_request_id: 'hitl_review_1',
        artifact_id: 'ia_review_1',
        action: 'continue',
      },
    });
    expect(onAccepted).toHaveBeenCalledOnce();
  });

  it('forwards the durable acceptance callback through the Turn orchestrator', async () => {
    const onAccepted = vi.fn();
    await runAgentTurn({
      wfId: 'scope_1',
      chatId: 'chat_accepted',
      content: 'hello',
      onAccepted,
    });
    expect(onAccepted).toHaveBeenCalledOnce();
  });

  it('reports a rejected active-turn race without treating it as sent', async () => {
    server.use(
      http.post('*/api/v1/chat-scopes/:scopeId/chats/:chatId/messages', () => (
        HttpResponse.json(
          { detail: { code: 'chat_run_active' } },
          { status: 409 },
        )
      )),
    );
    const onAccepted = vi.fn();
    const sent = await runAgentTurn({
      wfId: 'scope_1',
      chatId: 'chat_busy',
      content: 'keep this draft',
      onAccepted,
    });
    expect(sent).toBe(false);
    expect(onAccepted).not.toHaveBeenCalled();
  });

  it('detects a missing frame and resumes from the last projected sequence', async () => {
    let resumeCursor = '';
    server.use(
      http.post('*/api/v1/chat-scopes/:scopeId/chats/:chatId/messages', () => (
        new HttpResponse(
          [
            'id: 1\nevent: started\ndata: {"turn_id":"turn_gap_1"}\n\n',
            // seq=2 is deliberately absent from this connection.
            'id: 3\nevent: CHAT_EVENT\ndata: {"type":"message_delta","message_id":"assistant_gap","delta":"complete"}\n\n',
          ].join(''),
          {
            status: 200,
            headers: {
              'Content-Type': 'text/event-stream',
              'X-Turn-Id': 'turn_gap_1',
            },
          },
        )
      )),
      http.get('*/api/v1/chats/chat_gap/turns/turn_gap_1/stream', ({ request }) => {
        resumeCursor = request.headers.get('Last-Event-ID') ?? '';
        return new HttpResponse(
          [
            'id: 2\nevent: CHAT_EVENT\ndata: {"type":"message_start","message_id":"assistant_gap","role":"assistant"}\n\n',
            'id: 3\nevent: CHAT_EVENT\ndata: {"type":"message_delta","message_id":"assistant_gap","delta":"complete"}\n\n',
            'id: 4\nevent: done\ndata: {}\n\n',
          ].join(''),
          {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          },
        );
      }),
      http.get(
        '*/api/v1/chats/chat_gap/turns/by-client-request/:clientRequestId',
        () => HttpResponse.json({
          run_id: 'turn_gap_1',
          chat_id: 'chat_gap',
          status: 'running',
          last_event_id: 1,
          created_at: '2026-08-23T00:00:00Z',
          pending_hitl: [],
        }),
      ),
    );
    useChatStreamStore.getState().beginTurn('chat_gap', '');

    await streamAgentTurn({
      wfId: 'scope_gap',
      chatId: 'chat_gap',
      content: 'test continuity',
      signal: new AbortController().signal,
    });

    expect(resumeCursor).toBe('1');
    expect(useChatStreamStore.getState().runtimes.chat_gap.messages).toEqual([
      expect.objectContaining({
        id: 'assistant_gap',
        content: 'complete',
      }),
    ]);
  });

  it('uses the durable Run terminal state when the final SSE frame is lost', async () => {
    let resumeRequests = 0;
    server.use(
      http.post('*/api/v1/chat-scopes/:scopeId/chats/:chatId/messages', () => (
        new HttpResponse(
          [
            'id: 1\nevent: started\ndata: {"turn_id":"turn_terminal_1"}\n\n',
            'id: 2\nevent: CHAT_EVENT\ndata: {"type":"message_start","message_id":"assistant_terminal","role":"assistant"}\n\n',
            'id: 3\nevent: NO_OP\ndata: {}\n\n',
          ].join(''),
          {
            status: 200,
            headers: {
              'Content-Type': 'text/event-stream',
              'X-Turn-Id': 'turn_terminal_1',
            },
          },
        )
      )),
      http.get(
        '*/api/v1/chats/chat_terminal/turns/by-client-request/:clientRequestId',
        () => HttpResponse.json({
          run_id: 'turn_terminal_1',
          chat_id: 'chat_terminal',
          status: 'completed',
          last_event_id: 3,
          created_at: '2026-08-23T00:00:00Z',
          pending_hitl: [],
        }),
      ),
      http.get('*/api/v1/chats/chat_terminal/turns/turn_terminal_1/stream', () => {
        resumeRequests += 1;
        return new HttpResponse('id: 4\nevent: done\ndata: {}\n\n', {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        });
      }),
    );
    useChatStreamStore.getState().beginTurn('chat_terminal', '');

    await streamAgentTurn({
      wfId: 'scope_terminal',
      chatId: 'chat_terminal',
      content: 'finish even if the final frame is lost',
      signal: new AbortController().signal,
    });

    expect(resumeRequests).toBe(0);
    expect(useChatStreamStore.getState().runtimes.chat_terminal.state).toBe('complete');
  });
});
