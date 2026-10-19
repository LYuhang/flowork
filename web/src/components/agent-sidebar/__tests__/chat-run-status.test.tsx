import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { ChatRunStatus, ChatAliveIcon } from '../ChatRunStatus';
import { useChatStreamStore } from '@/stores/chat-stream';
import type { ChatListItem } from '@/lib/api/queries/chats';

type Summary = NonNullable<ChatListItem['latest_run']>;
const summary = (status: Summary['status'], run_id = 'turn'): Summary => ({
  run_id, status, unread: false, created_at: '2026-10-10T00:00:00Z', updated_at: '2026-10-10T00:00:00Z',
});
beforeEach(() => useChatStreamStore.setState({ runtimes: {} }));
afterEach(cleanup);

describe('ChatRunStatus', () => {
  it.each([
    ['running', 'Running'], ['waiting_approval', 'Needs your response'],
    ['cancel_requested', 'Stopping'],
    ['cancelled', 'Stopped'], ['failed', 'Failed'],
  ] as const)('restores %s without a locally opened stream', (status, label) => {
    render(<ChatRunStatus chatId="chat" run={summary(status)} />);
    expect(screen.getByRole('img', { name: label })).toHaveAttribute('data-chat-run-status', status);
  });
  it('does not decorate completed turns, whether read or unread', () => {
    const view = render(<ChatRunStatus chatId="chat" run={{ ...summary('completed'), unread: true }} />);
    expect(screen.queryByRole('img')).toBeNull();
    view.rerender(<ChatRunStatus chatId="chat" run={{ ...summary('completed'), unread: false }} />);
    expect(screen.queryByRole('img')).toBeNull();
  });
  it('does not invent status for an empty conversation', () => {
    render(<ChatRunStatus chatId="chat" />);
    expect(screen.queryByRole('img')).toBeNull();
  });
  it('follows a new live turn and its pending response instead of the old completed turn', () => {
    useChatStreamStore.getState().beginTurn('chat', 'new');
    render(<ChatRunStatus chatId="chat" run={summary('completed')} />);
    expect(screen.getByRole('img', { name: 'Running' })).toBeInTheDocument();
    act(() => useChatStreamStore.getState().setWaitingForUser(true, 'chat'));
    expect(screen.getByRole('img', { name: 'Needs your response' })).toBeInTheDocument();
  });
  it.each(['cancel_requested', 'completed', 'cancelled', 'failed'] as const)('prefers server %s over a lingering stream for that turn', status => {
    useChatStreamStore.getState().beginTurn('chat', 'turn');
    render(<ChatRunStatus chatId="chat" run={summary(status)} />);
    if (status === 'completed') expect(screen.queryByRole('img')).toBeNull();
    else expect(screen.getByRole('img')).toHaveAttribute('data-chat-run-status', status);
  });
  it('does not let an older failed local turn replace the current server turn', () => {
    useChatStreamStore.getState().beginTurn('chat', 'old');
    useChatStreamStore.getState().setState('failed', 'chat');
    render(<ChatRunStatus chatId="chat" run={summary('running')} />);
    expect(screen.getByRole('img', { name: 'Running' })).toBeInTheDocument();
  });
  it('removes the running indicator when the live turn completes', () => {
    useChatStreamStore.getState().beginTurn('chat', 'turn');
    render(<ChatRunStatus chatId="chat" run={summary('running')} />);
    act(() => useChatStreamStore.getState().setState('complete', 'chat'));
    expect(screen.queryByRole('img')).toBeNull();
  });
});


describe('chat alive outline', () => {
  it('combines serving and live run state without coloring idle sibling chats', () => {
    const view = render(<ChatAliveIcon chatId="chat" run={summary('completed')} serving />);
    expect(view.container.querySelector('svg')).toHaveAttribute('data-chat-alive', 'true');
    view.rerender(<ChatAliveIcon chatId="chat" run={summary('completed')} serving={false} />);
    expect(view.container.querySelector('svg')).toHaveAttribute('data-chat-alive', 'false');
    act(() => useChatStreamStore.getState().beginTurn('chat', 'new'));
    expect(view.container.querySelector('svg')).toHaveAttribute('data-chat-alive', 'true');
    act(() => useChatStreamStore.getState().setState('complete', 'chat'));
    expect(view.container.querySelector('svg')).toHaveAttribute('data-chat-alive', 'false');
    view.rerender(<ChatAliveIcon chatId="sibling" run={summary('completed')} serving={false} />);
    expect(view.container.querySelector('svg')).toHaveClass('text-content-tertiary');
  });
});
