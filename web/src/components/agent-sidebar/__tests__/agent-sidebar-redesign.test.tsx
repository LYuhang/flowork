import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { server } from '@/__tests__/msw-handlers';
import en from '@/lib/i18n/locales/en.json';
import zh from '@/lib/i18n/locales/zh.json';
import { ChatMessageList } from '@/components/agent-sidebar/ChatMessageList';
import { ChatHistoryMenu } from '@/components/agent-sidebar/ChatHistoryMenu';
import { AgentChatSidebar } from '@/components/agent-sidebar/AgentChatSidebar';
import { useChatStreamStore } from '@/stores/chat-stream';
import { useUIStore } from '@/stores/ui';
import i18n from '@/lib/i18n';

// ONE shared mock factory for the chats queries (isolate=false → one factory
// for the whole file; later tasks reuse historyMock/sessionsMock).
const historyMock = vi.fn();
const sessionsMock = vi.fn();
const fetchHistoryPageMock = vi.fn();
const readServerActiveTurnsMock = vi.fn(async (..._args: unknown[]) => [] as Array<{
  wfId: string;
  chatId: string;
  turnId: string;
}>);
vi.mock('@/lib/api/queries/chats', () => ({
  CHAT_INITIAL_HISTORY_LIMIT: 30,
  fetchChatHistory: vi.fn(),
  fetchChatHistoryPage: (...a: unknown[]) => fetchHistoryPageMock(...a),
  useChatHistory: (...a: unknown[]) => historyMock(...a),
  useChatSessions: (...a: unknown[]) => sessionsMock(...a),
  useChatWorkspace: (chatId: string | null) => ({
    data: chatId
      ? { chat_id: chatId, workspace_scope_id: `__chatws_test_${chatId}` }
      : undefined,
    isLoading: false,
  }),
  useChatBootstrap: () => ({
    data: { available_commands: [] },
    isLoading: false,
  }),
  useChatState: () => ({
    data: undefined,
    isFetched: true,
    isLoading: false,
  }),
}));
vi.mock('@/lib/api/sse/server-active-turn', () => ({
  readServerActiveTurns: (...args: unknown[]) => readServerActiveTurnsMock(...args),
}));
vi.mock('@/lib/api/sse/resume-turn', () => ({
  resumeActiveTurn: vi.fn(async () => undefined),
}));

beforeEach(async () => {
  fetchHistoryPageMock.mockReset();
  await i18n.changeLanguage('en');
});

function ChatQueryWrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe('agent sidebar i18n keys', () => {
  it('reuses existing new_chat + chat_history', () => {
    const e = en as Record<string, string>;
    expect(e['new_chat']).toBeTruthy();
    expect(e['chat_history']).toBeTruthy();
  });
  it('adds flat no_chats / no_messages (en + zh)', () => {
    const e = en as Record<string, string>;
    const z = zh as Record<string, string>;
    expect(e['no_chats']).toBe('No chats yet.');
    expect(e['no_messages']).toBe('No messages yet.');
    expect(z['no_chats']).toBe('暂无对话');
    expect(z['no_messages']).toBe('暂无消息');
  });
});

describe('ChatMessageList', () => {
  beforeEach(() => {
    historyMock.mockReset();
    sessionsMock.mockReset();
    sessionsMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    // The production store intentionally retains per-chat runtimes so a live
    // Turn survives chat switching. Tests must clear that durable in-memory
    // map, not only its legacy current-chat projection, or one test's active
    // Turn becomes another test's transcript base.
    useChatStreamStore.getState().reset();
    useUIStore.setState({ chatToolExpansion: {} });
    server.use(
      http.post('*/api/v1/previews/resolve', () =>
        HttpResponse.json({ detail: 'preview unavailable in this transcript test' }, { status: 404 })),
    );
  });

  it('no active chat → shows the concise empty-state invitation', () => {
    historyMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    render(<ChatMessageList wfId="wf" activeChatId={null} />);
    expect(screen.getByText('Send a message to start the conversation.')).toBeInTheDocument();
  });

  it('active + empty history (not streaming) → shows the invitation', () => {
    historyMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    render(<ChatMessageList wfId="wf" activeChatId="c1" />);
    expect(screen.getByText('Send a message to start the conversation.')).toBeInTheDocument();
  });

  it('loads an older page automatically when collapsed history cannot fill the viewport, even when animation frames are throttled', async () => {
    historyMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    const onLoadOlderHistory = vi.fn().mockResolvedValue(undefined);
    const animationFrame = vi
      .spyOn(window, 'requestAnimationFrame')
      .mockImplementation(() => 1);

    try {
      render(
        <ChatMessageList
          wfId="wf"
          activeChatId="c1"
          historyItems={[
            {
              role: 'assistant',
              content: '',
              tool_calls: [{ id: 'tc1', name: 'update_canvas', arguments: '{}' }],
            },
            {
              role: 'tool',
              tool_call_id: 'tc1',
              content: 'Canvas updated',
            },
          ] as never}
          hasOlderHistory
          onLoadOlderHistory={onLoadOlderHistory}
        />,
      );

      expect(screen.getByRole('button', { name: 'Load earlier messages' })).toBeInTheDocument();
      await waitFor(() => expect(onLoadOlderHistory).toHaveBeenCalledTimes(1));
    } finally {
      animationFrame.mockRestore();
    }
  });

  it('loads another history page when the user scrolls near the top', async () => {
    historyMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    const onLoadOlderHistory = vi.fn().mockResolvedValue(undefined);
    const props = {
      wfId: 'wf',
      activeChatId: 'c1',
      historyItems: [
        { id: 'recent-user', role: 'user', content: 'recent request' },
        { id: 'recent-agent', role: 'assistant', content: 'recent answer' },
      ] as never,
      hasOlderHistory: true,
      onLoadOlderHistory,
    };
    const { rerender } = render(
      <ChatMessageList {...props} olderHistoryLoading />,
    );
    const log = screen.getByRole('log', { name: 'Conversation' });
    Object.defineProperty(log, 'scrollHeight', { configurable: true, value: 1600 });
    Object.defineProperty(log, 'clientHeight', { configurable: true, value: 700 });
    Object.defineProperty(log, 'scrollTop', { configurable: true, value: 400, writable: true });

    rerender(<ChatMessageList {...props} olderHistoryLoading={false} />);
    await new Promise((resolve) => window.setTimeout(resolve, 20));
    expect(onLoadOlderHistory).not.toHaveBeenCalled();

    log.scrollTop = 40;
    fireEvent.scroll(log);
    await waitFor(() => expect(onLoadOlderHistory).toHaveBeenCalledTimes(1));
  });

  it('exposes a named conversation log and announces stream boundaries without token spam', async () => {
    historyMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    const { container } = render(<ChatMessageList wfId="wf" activeChatId="c1" />);

    const log = screen.getByRole('log', { name: 'Conversation' });
    expect(log).toHaveAttribute('aria-live', 'polite');
    expect(log).toHaveAttribute('aria-relevant', 'additions');
    const announcement = container.querySelector(
      '[data-role="agent-stream-announcement"]',
    );

    act(() => useChatStreamStore.getState().beginTurn('c1', 'turn-a11y'));
    await waitFor(() => expect(announcement).toHaveTextContent('Agent response started'));

    act(() => useChatStreamStore.getState().setState('complete', 'c1'));
    await waitFor(() => expect(announcement).toHaveTextContent('Agent response complete'));
  });

  it('does not query history for a draft chat while the session list is refetching', () => {
    sessionsMock.mockReturnValue({
      data: { items: [] },
      isLoading: false,
      isFetching: true,
    });
    historyMock.mockReturnValue({ data: undefined, isLoading: false });

    render(<ChatMessageList wfId="wf" activeChatId="draft-c1" />);

    expect(historyMock).toHaveBeenCalledWith('wf', null, false, null);
  });

  it('does not query history before the first turn has a server id', () => {
    sessionsMock.mockReturnValue({
      data: { items: [{ chat_id: 'c1', chat_context: 'New Chat' }] },
      isLoading: false,
      isFetching: false,
    });
    historyMock.mockReturnValue({ data: undefined, isLoading: false });
    useChatStreamStore.getState().beginTurn('c1', '');

    render(<ChatMessageList wfId="wf" activeChatId="c1" />);

    expect(historyMock).toHaveBeenCalledWith('wf', null, false, '');
  });

  it('keeps the running indicator below streaming assistant text', () => {
    historyMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    useChatStreamStore.getState().beginTurn('c1', 'turn-1');
    useChatStreamStore.getState().appendChunk(
      { role: 'assistant', content: 'live answer' },
      'c1',
    );
    render(<ChatMessageList wfId="wf" activeChatId="c1" />);
    expect(screen.queryByText('No messages yet.')).toBeNull();   // the C1 guard
    expect(screen.getByText('live answer')).toBeInTheDocument();
    expect(screen.getByLabelText(/agent is thinking/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/agent is thinking/i))
      .toHaveAttribute('data-message-role', 'assistant');

    act(() => useChatStreamStore.getState().setState('complete', 'c1'));
    expect(screen.queryByLabelText(/agent is thinking/i)).toBeNull();
  });

  it('shows thinking only while a streaming turn has no visible output', () => {
    historyMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    useChatStreamStore.getState().beginTurn('c1', 'turn-1');
    render(<ChatMessageList wfId="wf" activeChatId="c1" />);
    const thinking = screen.getByLabelText(/agent is thinking/i);
    expect(thinking.querySelector('[data-role="agent-startup-phase"]')).toBeNull();
    expect(thinking.querySelectorAll('.chat-thinking-dot')).toHaveLength(3);
  });

  it('stops the thinking animation while an active turn waits for user input', () => {
    historyMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    useChatStreamStore.getState().beginTurn('c1', 'turn-1');
    useChatStreamStore.getState().setWaitingForUser(true, 'c1');
    render(<ChatMessageList wfId="wf" activeChatId="c1" />);

    expect(useChatStreamStore.getState().runtimes.c1?.state).toBe('streaming');
    expect(screen.queryByLabelText(/agent is thinking/i)).toBeNull();
    expect(screen.queryByLabelText(/agent is still working/i)).toBeNull();
  });

  it('keeps startup progress text separate from the rotating activity indicator', async () => {
    historyMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    useChatStreamStore.getState().beginTurn('c1', 'turn-1');
    useChatStreamStore.getState().setStartupPhase('connecting_model', 'c1');
    render(<ChatMessageList wfId="wf" activeChatId="c1" />);

    const phase = await screen.findByText(/Connecting to model/);
    expect(phase).toHaveAttribute('data-role', 'agent-startup-phase');
    expect(phase).toHaveClass('leading-5', 'py-0.5');
    expect(phase).not.toHaveClass('leading-none');
    expect(phase).not.toHaveClass('truncate');
    expect(phase).not.toHaveTextContent(/…|\.\.\./);
    expect(
      screen.getByLabelText(/agent is thinking/i)
        .querySelector('[data-role="agent-thinking-dots"]'),
    ).not.toBeNull();
  });

  it.each([false, true])('renders progress without a bubble (compact=%s)', async (compact) => {
    historyMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    useChatStreamStore.getState().beginTurn('c1', 'turn-1');
    useChatStreamStore.getState().setStartupProgress({
      phase: 'running_tool',
      label: 'generating_jpg_preview_gjpyq',
      startedAt: new Date().toISOString(),
      firstTurn: false,
      runtimeType: 'codex',
    }, 'c1');
    render(<ChatMessageList wfId="wf" activeChatId="c1" compact={compact} />);

    const phase = await screen.findByText(/generating_jpg_preview_gjpyq/);
    expect(phase).toHaveClass('whitespace-normal', 'break-words', 'leading-5');
    expect(phase).not.toHaveClass('truncate', 'overflow-hidden', 'leading-none');
    const rail = phase.closest('[data-message-content-rail="assistant"]');
    expect(rail).toHaveClass('min-w-0', 'max-w-full');
    expect(rail?.className).not.toMatch(/(?:^|\s)(?:rounded|border|bg-|overflow-hidden)/);
    expect(screen.getByRole('status', { name: /agent is thinking/i })).toHaveAttribute('aria-live', 'polite');

    act(() => useChatStreamStore.getState().setStartupPhase('finalizing', 'c1'));
    expect(await screen.findByText(/Preparing response/)).toBeInTheDocument();
    act(() => useChatStreamStore.getState().setState('complete', 'c1'));
    expect(screen.queryByRole('status', { name: /agent is thinking/i })).toBeNull();
  });

  it('projects assistant deltas into the mounted transcript as they arrive', async () => {
    historyMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    useChatStreamStore.getState().beginTurn('c1', 'turn-1');
    render(<ChatMessageList wfId="wf" activeChatId="c1" historyItems={[]} />);

    act(() => {
      useChatStreamStore.getState().applyEvent({
        type: 'message_start',
        message_id: 'assistant-1',
        role: 'assistant',
      }, 'c1');
      useChatStreamStore.getState().applyEvent({
        type: 'message_delta',
        message_id: 'assistant-1',
        delta: 'Hel',
      }, 'c1');
    });
    await waitFor(() => expect(screen.getByText('Hel')).toBeInTheDocument());

    act(() => {
      useChatStreamStore.getState().applyEvent({
        type: 'message_delta',
        message_id: 'assistant-1',
        delta: 'lo',
      }, 'c1');
    });
    await waitFor(() => expect(screen.getByText('Hello')).toBeInTheDocument());
  });

  it('renders Markdown structure before the assistant stream has finished', async () => {
    historyMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    useChatStreamStore.getState().beginTurn('c1', 'turn-markdown');
    useChatStreamStore.getState().applyEvent({
      type: 'message_start',
      message_id: 'assistant-markdown',
      role: 'assistant',
    }, 'c1');
    useChatStreamStore.getState().applyEvent({
      type: 'message_replace',
      message_id: 'assistant-markdown',
      content: '## Live heading\n\n- first item',
    }, 'c1');

    render(<ChatMessageList wfId="wf" activeChatId="c1" historyItems={[]} />);

    expect(screen.getByRole('heading', { name: 'Live heading', level: 2 })).toBeInTheDocument();
    expect(screen.getByRole('listitem')).toHaveTextContent('first item');
    expect(document.querySelector('[data-role="markdown"][data-streaming="true"]')).not.toBeNull();
    expect(document.querySelector('[data-role="streaming-text"]')).toBeNull();
  });

  it('opens Agent VFS Markdown links in Preview without browser navigation', async () => {
    historyMock.mockReturnValue({
      data: {
        items: [{
          id: 'assistant-file-link',
          role: 'assistant',
          content: '工作流文件：[workflow.json](/data/workflow.json)',
        }],
      },
      isLoading: false,
    });
    sessionsMock.mockReturnValue({
      data: { items: [{ chat_id: 'c1', chat_context: 'Build' }] },
      isLoading: false,
    });
    const onOpenFilePreview = vi.fn();

    render(
      <ChatMessageList
        wfId="wf"
        activeChatId="c1"
        onOpenFilePreview={onOpenFilePreview}
      />,
      { wrapper: ChatQueryWrapper },
    );

    const link = screen.getByRole('link', { name: 'workflow.json' });
    expect(link).not.toHaveAttribute('target');
    expect(link).toHaveAttribute('data-action', 'open-file-preview');
    await userEvent.click(link);

    expect(onOpenFilePreview).toHaveBeenCalledOnce();
    expect(onOpenFilePreview).toHaveBeenCalledWith('/data/workflow.json');
  });

  it('keeps ordinary web Markdown links external', () => {
    historyMock.mockReturnValue({
      data: {
        items: [{
          id: 'assistant-web-link',
          role: 'assistant',
          content: '[OpenAI](https://openai.com/)',
        }],
      },
      isLoading: false,
    });
    sessionsMock.mockReturnValue({
      data: { items: [{ chat_id: 'c1', chat_context: 'Build' }] },
      isLoading: false,
    });

    render(
      <ChatMessageList
        wfId="wf"
        activeChatId="c1"
        onOpenFilePreview={vi.fn()}
      />,
    );

    const link = screen.getByRole('link', { name: 'OpenAI' });
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).not.toHaveAttribute('data-action', 'open-file-preview');
  });

  it('renders durable pre-turn history plus exactly one live Turn projection', async () => {
    useChatStreamStore.getState().reset();
    historyMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    const { rerender } = render(
      <ChatMessageList
        wfId="wf"
        activeChatId="c1"
        historyItems={[]}
      />,
    );

    act(() => {
      const store = useChatStreamStore.getState();
      store.beginTurn('c1', 'turn-1');
      store.markStarted('turn-1', 'c1');
      store.appendChunk({ role: 'user', content: 'one current user turn' }, 'c1');
    });
    await waitFor(() => {
      expect(screen.getAllByText('one current user turn')).toHaveLength(1);
    });

    act(() => {
      useChatStreamStore.getState().applyEvent({
        type: 'tool_start',
        message_id: 'assistant-tool-message',
        tool_call_id: 'tool-1',
        name: 'read_file',
        arguments: '{"path":"/data/a.txt"}',
      }, 'c1');
    });
    // The parent remains subscribed to checkpoint-before-turn while the live
    // projection owns the tail. Durable head is not mixed into this render.
    rerender(
      <ChatMessageList
        wfId="wf"
        activeChatId="c1"
        historyItems={[]}
      />,
    );

    expect(screen.getAllByText('one current user turn')).toHaveLength(1);
    expect(screen.getByRole('button', { name: /read_file/i })).toBeInTheDocument();
  });

  it('renders the completed current Turn once before and after durable-history handoff', () => {
    historyMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    const baseline = [
      { id: 'old-user', role: 'user', content: 'previous request' },
      { id: 'old-agent', role: 'assistant', content: 'previous answer' },
    ];
    const canonicalHead = [
      ...baseline,
      { id: 'current-user', role: 'user', content: 'current request' },
      { id: 'current-agent', role: 'assistant', content: 'current answer' },
    ];
    const stream = useChatStreamStore.getState();
    stream.beginTurn('c1', 'turn-current');
    stream.markStarted('turn-current', 'c1');
    stream.appendChunk({ role: 'user', content: 'current request' }, 'c1');
    stream.applyEvent({
      type: 'message_replace',
      message_id: 'current-agent',
      content: 'current answer',
    }, 'c1');
    stream.setState('complete', 'c1');

    const { rerender } = render(
      <ChatMessageList wfId="wf" activeChatId="c1" historyItems={baseline as never} />,
    );
    expect(screen.getAllByText('current request')).toHaveLength(1);
    expect(screen.getAllByText('current answer')).toHaveLength(1);

    act(() => {
      useChatStreamStore.getState().finishProjection('c1', 'turn-current');
      rerender(
        <ChatMessageList wfId="wf" activeChatId="c1" historyItems={canonicalHead as never} />,
      );
    });

    expect(screen.getAllByText('current request')).toHaveLength(1);
    expect(screen.getAllByText('current answer')).toHaveLength(1);
  });

  it('non-streaming history that merges to empty → shows the invitation', () => {
    // history is NON-empty but all-orphan tool chunks fold to zero via mergeChunks.
    historyMock.mockReturnValue({
      data: { items: [{ role: 'tool', tool_call_id: 'orphan', content: 'x' }] },
      isLoading: false,
    });
    render(<ChatMessageList wfId="wf" activeChatId="c1" />);
    expect(screen.getByText('Send a message to start the conversation.')).toBeInTheDocument();
  });

  it('renders assistant text as a plain content rail, separate from tool activity', () => {
    sessionsMock.mockReturnValue({
      data: { items: [{ chat_id: 'c1', chat_context: 'Build' }] },
      isLoading: false,
    });
    historyMock.mockReturnValue({
      data: {
        items: [
          {
            role: 'assistant',
            content: 'I will update it.',
            tool_calls: [{ id: 'tc1', name: 'vibe_workflow', arguments: '{}' }],
          },
          {
            role: 'tool',
            tool_call_id: 'tc1',
            content: '<!-- DIFF before → after -->\n~ /node_1',
            artifact: { status: 'success', meta: { tool: 'vibe_workflow' } },
          },
        ],
      },
      isLoading: false,
    });
    const { container } = render(<ChatMessageList wfId="wf" activeChatId="c1" />);

    const assistantResponse = container.querySelector('[data-message-surface="plain"]');
    expect(assistantResponse).toHaveTextContent('I will update it.');
    expect(assistantResponse).not.toHaveClass(
      'rounded-2xl',
      'border',
      'bg-surface-sunken/70',
    );
    expect(assistantResponse?.querySelector('[data-action="tool-activity-toggle"]')).toBeNull();
    expect(container.querySelector('[data-tool-activity="true"] [data-action="tool-activity-toggle"]')).toBeInTheDocument();

    const rails = Array.from(
      container.querySelectorAll<HTMLElement>('[data-message-content-rail="assistant"]'),
    );
    expect(rails).toHaveLength(2);
    // Text responses and tool activity use one 36px avatar rail and one 12px
    // gutter, so their content begins on exactly the same visual axis.
    for (const rail of rails) {
      const row = rail.parentElement;
      expect(row).toHaveClass('gap-3');
      expect(row?.firstElementChild).toHaveClass('h-9', 'w-9');
    }
  });

  it('groups adjacent tool activity into one block and spins while any call is running', () => {
    historyMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    const stream = useChatStreamStore.getState();
    stream.beginTurn('c1', 'turn-1');
    stream.appendChunk({
      role: 'assistant',
      content: '',
      tool_calls: [{ id: 'tc1', name: 'vibe_workflow', arguments: '{}' }],
    }, 'c1');
    stream.appendChunk({
      role: 'tool',
      tool_call_id: 'tc1',
      content: '<!-- DIFF before → after -->\n~ /node_1',
      artifact: { status: 'success', artifact: { handles: { workflow_id: 'wf_real' } } },
    }, 'c1');
    stream.appendChunk({
      role: 'assistant',
      content: '',
      tool_calls: [{ id: 'tc2', name: 'get_workflow', arguments: '{}' }],
    }, 'c1');

    const { container } = render(<ChatMessageList wfId="wf" activeChatId="c1" />);
    const groups = Array.from(container.querySelectorAll('[data-action="tool-activity-toggle"]'));
    expect(groups).toHaveLength(1);
    expect(groups[0]).toHaveTextContent('Running tools');
    expect(groups[0].querySelector('.animate-spin')).toBeInTheDocument();
  });

  it('keeps following tool calls in the same block when the first tool-call message has assistant text', () => {
    sessionsMock.mockReturnValue({
      data: { items: [{ chat_id: 'c1', chat_context: 'Build' }] },
      isLoading: false,
    });
    historyMock.mockReturnValue({
      data: {
        items: [
          {
            role: 'assistant',
            content: 'I will update the file and record the todo.',
            tool_calls: [{ id: 'tc1', name: 'write_file', arguments: '{"path":"/data/a.txt"}' }],
          },
          {
            role: 'tool',
            tool_call_id: 'tc1',
            content: 'Wrote /data/a.txt',
            artifact: { status: 'success', meta: { tool: 'write_file' } },
          },
          {
            role: 'assistant',
            content: '',
            tool_calls: [{ id: 'tc2', name: 'todo', arguments: '{"items":[]}' }],
          },
          {
            role: 'tool',
            tool_call_id: 'tc2',
            content: 'Todo updated',
            artifact: { status: 'success', meta: { tool: 'todo' } },
          },
        ],
      },
      isLoading: false,
    });

    const { container } = render(<ChatMessageList wfId="wf" activeChatId="c1" />);
    expect(screen.getByText('I will update the file and record the todo.')).toBeInTheDocument();
    const groups = Array.from(container.querySelectorAll('[data-action="tool-activity-toggle"]'));
    expect(groups).toHaveLength(1);
    expect(groups[0]).toHaveTextContent('2 tools used');
    expect(groups[0]).toHaveTextContent('todo');
    expect(groups[0]).not.toHaveTextContent('write_file -> todo');
  });

  it('preserves user-expanded tool activity when another tool call is appended', async () => {
    sessionsMock.mockReturnValue({
      data: { items: [{ chat_id: 'c1', chat_context: 'Build' }] },
      isLoading: false,
    });
    historyMock.mockReturnValue({ data: { items: [] }, isLoading: false });

    const first = [
      {
        role: 'assistant',
        content: '',
        tool_calls: [{ id: 'tc1', name: 'write_file', arguments: '{"path":"/data/a.txt"}' }],
      },
      {
        role: 'tool',
        tool_call_id: 'tc1',
        content: 'Wrote /data/a.txt',
        artifact: { status: 'success', meta: { tool: 'write_file' } },
      },
    ];
    const { rerender } = render(
      <ChatMessageList wfId="wf" activeChatId="c1" historyItems={first as never} />,
    );

    const firstGroup = screen.getByRole('button', { name: /1 tool used/i });
    await userEvent.click(firstGroup);
    expect(firstGroup).toHaveAttribute('aria-expanded', 'true');
    expect(document.querySelector('[data-role="tool-activity-details"]')).toBeInTheDocument();

    rerender(
      <ChatMessageList
        wfId="wf"
        activeChatId="c1"
        historyItems={[
          ...first,
          {
            role: 'assistant',
            content: '',
            tool_calls: [{ id: 'tc2', name: 'todo', arguments: '{"op":"done","id":1}' }],
          },
        ] as never}
      />,
    );

    const expandedGroup = screen.getByRole('button', { name: /2 tools used/i });
    expect(expandedGroup).toHaveAttribute('aria-expanded', 'true');
    expect(document.querySelector('[data-role="tool-activity-details"]')).toBeInTheDocument();
  });

  it('shows View workflow for a collapsed update_canvas tool group', async () => {
    sessionsMock.mockReturnValue({
      data: { items: [{ chat_id: 'c1', chat_context: 'Build' }] },
      isLoading: false,
    });
    historyMock.mockReturnValue({
      data: {
        items: [
          {
            role: 'assistant',
            content: '',
            tool_calls: [{ id: 'tc1', name: 'update_canvas', arguments: '{}' }],
          },
          {
            role: 'tool',
            tool_call_id: 'tc1',
            content: 'Canvas updated from /data/workflow.json',
            artifact: {
              status: 'success',
              artifact: { handles: { workflow_id: 'wf_real' } },
              meta: { tool: 'update_canvas' },
            },
          },
        ],
      },
      isLoading: false,
    });

    const onOpenWorkflowPreview = vi.fn();
    render(
      <ChatMessageList
        wfId="wf"
        activeChatId="c1"
        onOpenWorkflowPreview={onOpenWorkflowPreview}
      />,
    );
    const button = screen.getByRole('button', { name: /view workflow/i });
    await userEvent.click(button);
    expect(onOpenWorkflowPreview).toHaveBeenCalledWith('wf_real');
  });

  it('uses the current workflow fallback for update_canvas when the tool artifact has no workflow id', async () => {
    sessionsMock.mockReturnValue({
      data: { items: [{ chat_id: 'c1', chat_context: 'Build' }] },
      isLoading: false,
    });
    historyMock.mockReturnValue({
      data: {
        items: [
          {
            role: 'assistant',
            content: '',
            tool_calls: [{ id: 'tc1', name: 'update_canvas', arguments: '{}' }],
          },
          {
            role: 'tool',
            tool_call_id: 'tc1',
            content: 'Canvas updated: yes\nCanvas updated from /data/workflow.json',
            artifact: {
              status: 'success',
              meta: { tool: 'update_canvas' },
            },
          },
        ],
      },
      isLoading: false,
    });

    const onOpenWorkflowPreview = vi.fn();
    render(
      <ChatMessageList
        wfId="wf"
        activeChatId="c1"
        workflowViewerId="wf_fallback"
        onOpenWorkflowPreview={onOpenWorkflowPreview}
      />,
    );
    const button = screen.getByRole('button', { name: /view workflow/i });
    await userEvent.click(button);
    expect(onOpenWorkflowPreview).toHaveBeenCalledWith('wf_fallback');
  });

  it('keeps a tool running when the matching tool_end protocol frame is missing', () => {
    historyMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    const stream = useChatStreamStore.getState();
    stream.beginTurn('c1', 'turn-1');
    stream.applyEvent({
      type: 'tool_start',
      message_id: 'toolmsg:tc_create',
      tool_call_id: 'tc_create',
      name: 'create_workflow',
      arguments: '{"name":"Demo"}',
    }, 'c1');

    const { container } = render(<ChatMessageList wfId="wf" activeChatId="c1" />);
    const group = container.querySelector('[data-action="tool-activity-toggle"]');
    expect(group).toBeInTheDocument();
    expect(group?.querySelector('.animate-spin')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /view workflow/i })).toBeNull();
  });

  it('does not show View workflow for get_workflow tool groups', () => {
    sessionsMock.mockReturnValue({
      data: { items: [{ chat_id: 'c1', chat_context: 'Build' }] },
      isLoading: false,
    });
    historyMock.mockReturnValue({
      data: {
        items: [
          {
            role: 'assistant',
            content: '',
            tool_calls: [{ id: 'tc1', name: 'get_workflow', arguments: '{}' }],
          },
          {
            role: 'tool',
            tool_call_id: 'tc1',
            content: 'Exported current canvas workflow to /data/workflow.json',
            artifact: {
              status: 'success',
              artifact: { handles: { workflow_id: 'wf_real' } },
              meta: { tool: 'get_workflow' },
            },
          },
        ],
      },
      isLoading: false,
    });

    render(<ChatMessageList wfId="wf" activeChatId="c1" />);
    expect(screen.queryByRole('button', { name: /view workflow/i })).toBeNull();
  });

  it('renders stable history behind a resumed streaming turn', () => {
    sessionsMock.mockReturnValue({
      data: { items: [{ chat_id: 'c1', chat_context: 'Build' }] },
      isLoading: false,
    });
    historyMock.mockReturnValue({
      data: {
        items: [
          { role: 'user', content: 'previous request' },
          { role: 'assistant', content: 'previous answer' },
        ],
      },
      isLoading: false,
      isFetching: false,
      isError: false,
    });
    const stream = useChatStreamStore.getState();
    stream.beginTurn('c1', 'turn-resumed');
    stream.applyEvent({
      type: 'tool_start',
      message_id: 'toolmsg:tc_create',
      tool_call_id: 'tc_create',
      name: 'create_workflow',
      arguments: '{"name":"Demo"}',
    }, 'c1');

    const { container } = render(<ChatMessageList wfId="wf" activeChatId="c1" />);

    expect(screen.getByText('previous request')).toBeInTheDocument();
    expect(screen.getByText('previous answer')).toBeInTheDocument();
    expect(container.querySelector('[data-action="tool-activity-toggle"]')).toBeInTheDocument();
  });

  it('renders View workflow only on the update_canvas tool group, not inside each tool block', async () => {
    sessionsMock.mockReturnValue({
      data: { items: [{ chat_id: 'c1', chat_context: 'Build' }] },
      isLoading: false,
    });
    historyMock.mockReturnValue({
      data: {
        items: [
          {
            role: 'assistant',
            content: '',
            tool_calls: [{ id: 'tc1', name: 'update_canvas', arguments: '{}' }],
          },
          {
            role: 'tool',
            tool_call_id: 'tc1',
            content: 'Canvas updated from /data/workflow.json',
            artifact: {
              status: 'success',
              artifact: { handles: { workflow_id: 'wf_real' } },
              meta: { tool: 'update_canvas' },
            },
          },
        ],
      },
      isLoading: false,
    });

    const onOpenWorkflowPreview = vi.fn();
    render(
      <ChatMessageList
        wfId="wf"
        activeChatId="c1"
        onOpenWorkflowPreview={onOpenWorkflowPreview}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: /1 tool used/i }));
    expect(screen.getAllByRole('button', { name: /view workflow/i })).toHaveLength(1);
  });
});

describe('ChatHistoryMenu', () => {
  beforeEach(() => {
    sessionsMock.mockReset();
  });

  it('empty → "No chats yet."', async () => {
    sessionsMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    render(<ChatHistoryMenu wfId="wf" activeChatId={null} onSelect={() => {}} />);
    await userEvent.click(screen.getByRole('button', { name: /chat history/i }));
    expect(await screen.findByText('No chats yet.')).toBeInTheDocument();
  });

  it('lists sessions; clicking one calls onSelect(chat_id)', async () => {
    sessionsMock.mockReturnValue({
      data: { items: [
        { chat_id: 'c1', chat_context: 'Research' },
        { chat_id: 'c2', chat_context: '' },
      ] },
      isLoading: false,
    });
    const onSelect = vi.fn();
    render(<ChatHistoryMenu wfId="wf" activeChatId="c1" onSelect={onSelect} />);
    await userEvent.click(screen.getByRole('button', { name: /chat history/i }));
    await userEvent.click(await screen.findByText('Research'));
    expect(onSelect).toHaveBeenCalledWith('c1');
  });

  it('requests only browser history for the extension surface', () => {
    sessionsMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    render(
      <ChatHistoryMenu
        wfId="browser-scope"
        activeChatId={null}
        onSelect={() => {}}
        surface="browser"
      />,
    );
    expect(sessionsMock).toHaveBeenCalledWith('browser-scope', 'browser');
  });
});

// AgentChatSidebar mounts AgentSettingsModal, which calls `useLlmCredentials`
// (a `useQuery`) — so the sidebar needs a QueryClientProvider in addition to
// the router. One wrapper supplies both.
function SidebarWrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

describe('AgentChatSidebar redesign', () => {
  beforeEach(() => {
    readServerActiveTurnsMock.mockReset();
    readServerActiveTurnsMock.mockResolvedValue([]);
    useUIStore.setState({ lastActiveWorkflowId: 'wf', activeChatIds: { chat: null, browser: null } });
    sessionsMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    historyMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    useChatStreamStore.getState().reset();
  });

  it('header has New Chat + History + Close', () => {
    render(<AgentChatSidebar />, { wrapper: SidebarWrapper });
    expect(screen.getByRole('button', { name: /new chat/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /chat history/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /close agent sidebar/i })).toBeInTheDocument();
  });

  it('clicking New Chat sets a fresh activeChatId', async () => {
    render(<AgentChatSidebar />, { wrapper: SidebarWrapper });
    await userEvent.click(screen.getByRole('button', { name: /new chat/i }));
    const id = useUIStore.getState().activeChatIds.chat;
    expect(typeof id === 'string' && id.length > 0).toBe(true);
  });

  it('does not let delayed startup discovery replace an explicit New Chat', async () => {
    let finishDiscovery!: (turns: Array<{
      wfId: string;
      chatId: string;
      turnId: string;
    }>) => void;
    readServerActiveTurnsMock.mockImplementationOnce(() => new Promise((resolve) => {
      finishDiscovery = resolve;
    }));
    render(<AgentChatSidebar />, { wrapper: SidebarWrapper });
    await waitFor(() => expect(readServerActiveTurnsMock).toHaveBeenCalled());

    await userEvent.click(screen.getByRole('button', { name: /new chat/i }));
    const chosen = useUIStore.getState().activeChatIds.chat;
    expect(chosen).toBeTruthy();

    await act(async () => {
      finishDiscovery([{
        wfId: 'wf',
        chatId: 'older-running-chat',
        turnId: 'turn-old',
      }]);
      await Promise.resolve();
    });

    expect(useUIStore.getState().activeChatIds.chat).toBe(chosen);
  });

  it('restores the latest persisted browser Chat after Sidepanel reload', async () => {
    sessionsMock.mockReturnValue({
      data: {
        items: [
          { chat_id: 'browser-latest', chat_context: 'Pending review' },
          { chat_id: 'browser-older', chat_context: 'Older chat' },
        ],
      },
      isLoading: false,
      isFetched: true,
    });
    render(
      <AgentChatSidebar embedded chatSurface="browser" />,
      { wrapper: SidebarWrapper },
    );
    await waitFor(() => {
      expect(useUIStore.getState().activeChatIds.browser).toBe('browser-latest');
    });
  });

  it('no 140px session rail remains (full-width conversation)', () => {
    const { container } = render(<AgentChatSidebar />, { wrapper: SidebarWrapper });
    expect(container.querySelector('.w-\\[140px\\]')).toBeNull();
    expect(container.querySelector('[data-role="agent-message-list"]')).toBeInTheDocument();
  });

  it('keeps durable history mounted when a follow-up Turn starts', () => {
    useUIStore.setState({
      lastActiveWorkflowId: 'wf',
      activeChatIds: { chat: 'c1', browser: null },
    });
    sessionsMock.mockReturnValue({
      data: { items: [{ chat_id: 'c1', chat_context: 'Existing chat' }] },
      isLoading: false,
      isFetched: true,
    });
    const persistedHistory = {
      data: {
        items: [
          { id: 'old-user', role: 'user', content: 'previous request' },
          { id: 'old-agent', role: 'assistant', content: 'previous answer' },
        ],
      },
      isLoading: false,
      isError: false,
    };
    const disabledHistory = { data: undefined, isLoading: false, isError: false };
    historyMock.mockImplementation(
      (_scopeId: string, chatId: string | null) =>
        chatId ? persistedHistory : disabledHistory,
    );
    render(<AgentChatSidebar />, { wrapper: SidebarWrapper });
    expect(screen.getByText('previous request')).toBeInTheDocument();
    expect(screen.getByText('previous answer')).toBeInTheDocument();

    act(() => {
      useChatStreamStore.getState().beginTurn('c1', '');
      useChatStreamStore.getState().appendChunk(
        { role: 'user', content: 'follow-up request' },
        'c1',
      );
    });

    expect(screen.getByText('previous request')).toBeInTheDocument();
    expect(screen.getByText('previous answer')).toBeInTheDocument();
    expect(screen.getByText('follow-up request')).toBeInTheDocument();
  });

  it('excludes the active Turn from Sidepanel history while SSE owns its live projection', () => {
    useUIStore.setState({
      lastActiveWorkflowId: 'wf',
      activeChatIds: { chat: null, browser: 'browser-active' },
    });
    sessionsMock.mockReturnValue({
      data: { items: [{ chat_id: 'browser-active', chat_context: 'Browser run' }] },
      isLoading: false,
      isFetched: true,
    });
    historyMock.mockReturnValue({
      data: { items: [] },
      isLoading: false,
      isError: false,
    });
    useChatStreamStore.getState().beginTurn('browser-active', 'turn-live');

    render(
      <AgentChatSidebar embedded chatSurface="browser" />,
      { wrapper: SidebarWrapper },
    );

    expect(historyMock).toHaveBeenCalledWith(
      'wf',
      'browser-active',
      true,
      'turn-live',
    );
  });

  it('loads browser messages older than a tool-heavy tail instead of hiding prior turns', async () => {
    useUIStore.setState({
      lastActiveWorkflowId: 'wf',
      activeChatIds: { chat: null, browser: 'browser-long' },
    });
    sessionsMock.mockReturnValue({
      data: { items: [{ chat_id: 'browser-long', chat_context: 'Long browser chat' }] },
      isLoading: false,
      isFetched: true,
    });
    historyMock.mockReturnValue({
      data: {
        items: Array.from({ length: 30 }, (_, index) => ({
          id: `recent-tool-${index}`,
          role: 'assistant',
          content: `recent browser step ${index}`,
        })),
        total: 61,
        limit: 30,
        offset: 31,
      },
      isLoading: false,
      isError: false,
    });
    fetchHistoryPageMock.mockImplementation(
      (_scopeId: string, _chatId: string, options: { limit: number; offset: number }) =>
        Promise.resolve({
          items: options.offset === 0
            ? [{ id: 'oldest-user', role: 'user', content: 'create this browser session' }]
            : [
                { id: 'first-user', role: 'user', content: 'open the browser acceptance page' },
                { id: 'first-agent', role: 'assistant', content: 'starting browser control' },
              ],
          total: 61,
          limit: options.limit,
          offset: options.offset,
        }),
    );

    render(
      <AgentChatSidebar embedded chatSurface="browser" />,
      { wrapper: SidebarWrapper },
    );

    await waitFor(() => expect(fetchHistoryPageMock).toHaveBeenCalledWith(
      'wf',
      'browser-long',
      { limit: 30, offset: 1 },
    ));
    expect(await screen.findByText('open the browser acceptance page')).toBeInTheDocument();
    await waitFor(() => expect(fetchHistoryPageMock).toHaveBeenCalledWith(
      'wf',
      'browser-long',
      { limit: 1, offset: 0 },
    ));
    expect(await screen.findByText('create this browser session')).toBeInTheDocument();
  });
});
