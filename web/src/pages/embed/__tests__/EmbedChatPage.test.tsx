import { act, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { EmbedChatPage } from '../EmbedChatPage';
import { useAuthStore } from '@/stores/auth';
import { useUIStore } from '@/stores/ui';
import { mintBrowserToken } from '@/lib/api/browser';
import { reconcileChatWithServer } from '@/lib/api/sse/chat-reconcile';
import { cancelActiveTurn } from '@/lib/api/cancel-turn';

const browserHistory = vi.hoisted(() => ({ empty: false }));

vi.mock('next-themes', () => ({ useTheme: () => ({ resolvedTheme: 'light' }) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
vi.mock('@/lib/extension', () => ({ extensionOrigin: () => 'chrome-extension://test' }));
vi.mock('@/lib/api/browser', () => ({ mintBrowserToken: vi.fn().mockResolvedValue('scoped-token') }));
vi.mock('@/lib/api/cancel-turn', () => ({ cancelActiveTurn: vi.fn() }));
vi.mock('@/lib/api/sse/chat-reconcile', () => ({ reconcileChatWithServer: vi.fn() }));
vi.mock('@/pages/embed/EmbedLogin', () => ({ EmbedLogin: () => <div>Login</div> }));
vi.mock('@/lib/api/queries/chats', () => ({
  useBrowserChatBootstrap: () => ({ data: {
    carrier_scope_id: `scope-${useAuthStore.getState().user?.user_id}`,
  }, isLoading: false }),
  useChatSessions: () => ({ data: { items: browserHistory.empty ? [] : [{
    chat_id: `chat-${useAuthStore.getState().user?.user_id}`,
  }] }, isFetched: true }),
}));
vi.mock('@/pages/embed/EmbedShell', () => ({ EmbedShell: ({ wfId }: { wfId: string }) => {
  const chat = useUIStore((s) => s.activeChatIds.browser);
  return <div data-testid="shell">{wfId}:{chat}</div>;
} }));

function identity(userId: string, tenantId = 'tenant') {
  return { authenticated: true, sessionAudience: 'extension', user: {
    user_id: userId, tenant_id: tenantId, email: `${userId}@example.test`, displayName: userId,
  } };
}

async function shellMessage(data: Record<string, unknown>) {
  await act(async () => {
    window.dispatchEvent(new MessageEvent('message', {
      data, origin: 'chrome-extension://test', source: window.parent,
    }));
  });
}

async function bind() {
  await shellMessage({ type: 'BINDING', browser_id: 'browser', browser_control_available_here: true });
  await shellMessage({ type: 'OPEN_WS_RESULT', ok: true });
  await waitFor(() => expect(screen.getByTestId('shell')).toBeInTheDocument());
}

beforeEach(() => {
  vi.clearAllMocks();
  browserHistory.empty = false;
  useAuthStore.setState({ ...identity('first'), bootstrap: vi.fn().mockResolvedValue(undefined) });
  useUIStore.setState({ activeChatIds: { chat: null, browser: null } });
});

describe('embedded Chat identity boundary', () => {
  it('delegates empty-history creation to the sidebar without a frontend-only Chat ID', async () => {
    browserHistory.empty = true;
    render(<MemoryRouter><EmbedChatPage /></MemoryRouter>);
    await bind();
    expect(screen.getByTestId('shell')).toHaveTextContent('scope-first:');
    expect(useUIStore.getState().activeChatIds.browser).toBeNull();
  });

  it('reconciles the selected Chat after New Chat, not the initial handoff Chat', async () => {
    render(<MemoryRouter initialEntries={['/embed/chat?chat=initial-chat']}><EmbedChatPage /></MemoryRouter>);
    await bind();
    act(() => useUIStore.getState().setActiveChatId('browser', 'new-chat'));
    await shellMessage({ type: 'BROWSER_SESSION_CHANGED', status: 'released' });
    expect(reconcileChatWithServer).toHaveBeenLastCalledWith({
      wfId: 'scope-first', chatId: 'new-chat', surface: 'browser',
    });
    await shellMessage({ type: 'BROWSER_STOP_REQUESTED' });
    expect(cancelActiveTurn).toHaveBeenLastCalledWith('new-chat');
  });

  it('drops local Chat and URL handoff hints on account switch without a page reload', async () => {
    render(<MemoryRouter initialEntries={['/embed/chat?wf=handoff-scope&chat=handoff-chat']}><EmbedChatPage /></MemoryRouter>);
    await bind();
    expect(screen.getByTestId('shell')).toHaveTextContent('handoff-scope:handoff-chat');
    await act(async () => {
      useUIStore.setState({ activeChatIds: { chat: null, browser: null } });
      useAuthStore.setState(identity('second'));
    });
    expect(screen.queryByTestId('shell')).not.toBeInTheDocument();
    await bind();
    expect(screen.getByTestId('shell')).toHaveTextContent('scope-second:chat-second');
    expect(mintBrowserToken).toHaveBeenLastCalledWith('scope-second', 'browser');
  });

  it('preserves the selected Chat during same-account authentication renewal', async () => {
    render(<MemoryRouter initialEntries={['/embed/chat?chat=selected-chat']}><EmbedChatPage /></MemoryRouter>);
    await bind();
    const calls = vi.mocked(mintBrowserToken).mock.calls.length;
    await act(async () => { useAuthStore.setState(identity('first')); });
    expect(screen.getByTestId('shell')).toHaveTextContent('scope-first:selected-chat');
    expect(mintBrowserToken).toHaveBeenCalledTimes(calls);
  });

  it('keeps the same mounted shell while the socket capability renews', async () => {
    render(<MemoryRouter><EmbedChatPage /></MemoryRouter>);
    await bind();
    const shell = screen.getByTestId('shell');
    await shellMessage({ type: 'BROWSER_TRANSPORT_STATE', connected: true });
    await shellMessage({ type: 'BROWSER_WS_AUTH_REQUIRED' });
    expect(screen.getByTestId('shell')).toBe(shell);
    await shellMessage({ type: 'OPEN_WS_RESULT', ok: true, connected: true });
    expect(screen.getByTestId('shell')).toBe(shell);
  });

  it('preserves initial URL handoff while cold authentication resolves', async () => {
    useAuthStore.setState({ authenticated: false, user: null, sessionAudience: null });
    render(<MemoryRouter initialEntries={['/embed/chat?wf=handoff-scope&chat=handoff-chat']}><EmbedChatPage /></MemoryRouter>);
    await act(async () => { useAuthStore.setState(identity('first')); });
    await bind();
    expect(screen.getByTestId('shell')).toHaveTextContent('handoff-scope:handoff-chat');
  });

  it('clears local selection across logout and login to another account', async () => {
    render(<MemoryRouter><EmbedChatPage /></MemoryRouter>);
    await bind();
    expect(screen.getByTestId('shell')).toHaveTextContent('chat-first');
    await act(async () => {
      useAuthStore.setState({ authenticated: false, user: null, sessionAudience: null });
      useUIStore.setState({ activeChatIds: { chat: null, browser: null } });
    });
    expect(screen.getByText('Login')).toBeInTheDocument();
    await act(async () => { useAuthStore.setState(identity('second')); });
    await bind();
    expect(screen.getByTestId('shell')).toHaveTextContent('scope-second:chat-second');
  });

  it('resets local URL selection when the same user changes organization', async () => {
    render(<MemoryRouter initialEntries={['/embed/chat?chat=old-org-chat']}><EmbedChatPage /></MemoryRouter>);
    await bind();
    await act(async () => {
      useUIStore.setState({ activeChatIds: { chat: null, browser: null } });
      useAuthStore.setState(identity('first', 'another-tenant'));
    });
    await bind();
    expect(screen.getByTestId('shell')).toHaveTextContent('scope-first:chat-first');
  });

  it('does not send a token minted for the previous account after identity changes', async () => {
    const host = document.createElement('iframe');
    document.body.append(host);
    const parent = host.contentWindow!;
    const parentGetter = vi.spyOn(window, 'parent', 'get').mockReturnValue(parent);
    const post = vi.spyOn(parent, 'postMessage').mockImplementation(() => {});
    let finishOldMint!: (token: string) => void;
    vi.mocked(mintBrowserToken).mockReturnValueOnce(new Promise<string>((resolve) => { finishOldMint = resolve; }));
    const view = render(<MemoryRouter><EmbedChatPage /></MemoryRouter>);
    try {
      await shellMessage({ type: 'BINDING', browser_id: 'browser' });
      await waitFor(() => expect(mintBrowserToken).toHaveBeenCalledWith('scope-first', 'browser'));
      await act(async () => {
        useUIStore.setState({ activeChatIds: { chat: null, browser: null } });
        useAuthStore.setState(identity('second'));
        finishOldMint('old-account-token');
      });
      expect(post.mock.calls.some(([message]) => message?.type === 'OPEN_WS' && message.scopedToken === 'old-account-token')).toBe(false);
      await bind();
      expect(screen.getByTestId('shell')).toHaveTextContent('scope-second:chat-second');
      expect(post.mock.calls.some(([message]) => message?.type === 'OPEN_WS' && message.scopedToken === 'scoped-token')).toBe(true);
    } finally {
      view.unmount();
      post.mockRestore();
      parentGetter.mockRestore();
      host.remove();
    }
  });
});
