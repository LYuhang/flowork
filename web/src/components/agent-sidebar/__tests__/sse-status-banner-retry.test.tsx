import { cancelActiveTurn } from '@/lib/api/cancel-turn';
vi.mock('@/lib/api/cancel-turn', () => ({ cancelActiveTurn: vi.fn().mockResolvedValue(undefined) }));
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { SSEStatusBanner } from '@/components/agent-sidebar/SSEStatusBanner';
import { useChatStreamStore } from '@/stores/chat-stream';

const runAgentTurnMock = vi.fn().mockResolvedValue(undefined);

describe('SSEStatusBanner retry protocol', () => {
  beforeEach(() => {
    runAgentTurnMock.mockClear();
    vi.mocked(cancelActiveTurn).mockClear();
    useChatStreamStore.getState().reset();
  });

  it('replays the complete browser Turn policy and surface snapshot without browser topology', async () => {
    const store = useChatStreamStore.getState();
    store.beginTurn('chat_browser', 'turn_interrupted');
    store.setLastInput({
      content: 'continue on the current page',
      mode: 'browser',
      surface: 'sidepanel',
      agentSurface: 'browser',
      approvalMode: 'always_allow',
    }, 'chat_browser');
    store.setState('interrupted', 'chat_browser');

    render(
      <SSEStatusBanner
        wfId="scope_1"
        activeChatId="chat_browser"
        runTurn={runAgentTurnMock}
      />,
    );
    expect(runAgentTurnMock).not.toHaveBeenCalled();
    expect(screen.getByRole('status')).toHaveTextContent(/new execution/i);
    await userEvent.click(screen.getByRole('button', { name: /retry last request|重新执行上次请求/i }));

    expect(runAgentTurnMock).toHaveBeenCalledWith({
      wfId: 'scope_1',
      chatId: 'chat_browser',
      content: 'continue on the current page',
      attachments: undefined,
      mode: 'browser',
      surface: 'sidepanel',
      agentSurface: 'browser',
      approvalMode: 'always_allow',
    });
  });

  it('requests backend cancellation without claiming the interrupted turn is idle', async () => {
    const store = useChatStreamStore.getState();
    store.beginTurn('chat_stop', 'turn_stop');
    store.setState('interrupted', 'chat_stop');
    render(<SSEStatusBanner wfId="scope_1" activeChatId="chat_stop" runTurn={runAgentTurnMock} />);
    await userEvent.click(screen.getByRole('button', { name: /cancel turn|取消本轮/i }));
    expect(cancelActiveTurn).toHaveBeenCalledWith('chat_stop');
    expect(useChatStreamStore.getState().runtimes.chat_stop.state).toBe('interrupted');
    expect(runAgentTurnMock).not.toHaveBeenCalled();
  });

  it('does not describe a Runtime failure as a reconnection failure', () => {
    const store = useChatStreamStore.getState();
    store.beginTurn('chat_failed', 'turn_failed');
    store.setState('failed', 'chat_failed');

    render(
      <SSEStatusBanner
        wfId="scope_1"
        activeChatId="chat_failed"
        runTurn={runAgentTurnMock}
      />,
    );

    expect(screen.getByRole('status')).toHaveTextContent(/agent run failed/i);
    expect(screen.getByRole('status')).not.toHaveTextContent(/connection interrupted/i);
  });
});
