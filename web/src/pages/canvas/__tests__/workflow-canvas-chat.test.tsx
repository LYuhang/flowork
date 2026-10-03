import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { WorkflowCanvasChat } from '../WorkflowCanvasChat';
import { useOpenCanvasChat } from '../CanvasChatContext';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import type { ChatComposerProps } from '@/components/agent-sidebar/ChatComposer';
import type { ChatListItem } from '@/lib/api/queries/chats';

const mocks = vi.hoisted(() => ({
  create: vi.fn(), commit: vi.fn(), reconcile: vi.fn(),
  items: [] as ChatListItem[], history: vi.fn(),
}));
vi.mock('@/lib/api/queries/chats', () => ({
  useChatSessions: () => ({ data: { items: mocks.items }, isLoading: false }),
  useChatWorkspace: () => ({ data: { workspace_scope_id: 'wf' } }),
  useCreateChatSession: () => ({ mutateAsync: mocks.create }),
}));
vi.mock('@/lib/api/mutations/workflow-ops', () => ({ useCommitWorkflow: () => ({ mutateAsync: mocks.commit }) }));
vi.mock('@/lib/chat/use-conversation-history', () => ({ useConversationHistory: (...args: unknown[]) => {
  mocks.history(...args);
  return { ready: true, query: {}, items: [], loadOlder: vi.fn() };
} }));
vi.mock('@/lib/api/sse/chat-reconcile', () => ({ CHAT_RECONCILE_INTERVAL_MS: 30000, reconcileChatWithServer: mocks.reconcile }));
vi.mock('@/components/agent-sidebar/ChatComposer', () => ({ ChatComposer: (props: ChatComposerProps) =>
  <button disabled={!!props.disabledReason} onClick={async () => {
    try { await props.prepareConversation?.(); props.onSendAccepted?.(); } catch { /* Error retained for retry. */ }
  }}>Send contextual message</button> }));
vi.mock('@/components/agent-sidebar/ChatMessageList', () => ({ ChatMessageList: () => <div>Transcript</div> }));
vi.mock('@/components/agent-sidebar/SSEStatusBanner', () => ({ SSEStatusBanner: () => null }));
vi.mock('@/components/agent-sidebar/ChatHistoryMenu', () => ({ ChatHistoryMenu: ({ onSelect }: { onSelect: (id: string) => void }) =>
  <button onClick={() => onSelect('history')}>Choose history</button> }));

function Launcher() {
  const open = useOpenCanvasChat();
  return <>
    <button onClick={() => open?.({ kind: 'node', node_id: 'code' }, { x: 50, y: 80 })}>Node context</button>
    <div className="react-flow__pane" data-testid="canvas" />
  </>;
}
function mount(readOnly = false) {
  return render(<WorkflowCanvasChat wfId="wf" readOnly={readOnly}><Launcher /></WorkflowCanvasChat>);
}
beforeEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  mocks.items = [];
  useWorkflowEditStore.getState().setDraft({ code: { node_id: 'code', node_type: 'CodeNode', children: [] } }, 'v2.sv3');
  mocks.create.mockImplementation(async ({ chatId, workflowContext }) => ({
    chat_id: chatId, project_id: 'internal-project', scope_id: 'wf', workflow_context: workflowContext,
  }));
  mocks.commit.mockImplementation(async workflow => {
    useWorkflowEditStore.getState().markSaved('v2.sv4', workflow);
    return { active_v: 2, active_sv: 4 };
  });
});

describe('Workflow contextual conversation lifecycle', () => {
  it('opens locally, dismisses an unsent conversation and never creates server resources', () => {
    mount();
    fireEvent.click(screen.getByText('Node context'));
    expect(screen.getByRole('dialog')).toHaveAccessibleName('[v2.sv3] Node code');
    expect(mocks.create).not.toHaveBeenCalled();
    expect(mocks.reconcile).not.toHaveBeenCalled();
    fireEvent.pointerDown(screen.getByTestId('canvas'));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it('saves a dirty canvas first and binds the version returned by Save', async () => {
    useWorkflowEditStore.getState().applyEdit(wf => ({ ...wf, code: { node_id: 'code', node_type: 'CodeNode', children: [], node_name: 'Changed' } }));
    mount();
    fireEvent.click(screen.getByText('Node context'));
    fireEvent.click(screen.getByText('Send contextual message'));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledOnce());
    expect(mocks.commit).toHaveBeenCalledOnce();
    expect(mocks.commit.mock.invocationCallOrder[0]).toBeLessThan(mocks.create.mock.invocationCallOrder[0]);
    expect(mocks.create.mock.calls[0][0].workflowContext).toEqual({ workflow_id: 'wf', major_version: 2,
      initial_subversion: 4, target: { kind: 'node', node_id: 'code' } });
    fireEvent.pointerDown(screen.getByTestId('canvas'));
    fireEvent.click(screen.getByRole('button', { name: 'Workflow conversations' }));
    expect(screen.getByText('Transcript')).toBeInTheDocument();
    expect(mocks.create).toHaveBeenCalledOnce();
  });

  it('keeps the unsent conversation when Save rejects a version conflict', async () => {
    useWorkflowEditStore.getState().applyEdit(wf => ({ ...wf, code: { node_id: 'code', node_type: 'CodeNode', children: [], node_name: 'Changed' } }));
    mocks.commit.mockRejectedValue(new Error('version conflict'));
    mount();
    fireEvent.click(screen.getByText('Node context'));
    fireEvent.click(screen.getByText('Send contextual message'));
    await waitFor(() => expect(mocks.commit).toHaveBeenCalledOnce());
    expect(mocks.create).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toHaveAccessibleName('[v2.sv3] Node Changed');
  });

  it('resumes history with its original identity and major after refresh', async () => {
    mocks.items = [{ chat_id: 'history', project_id: 'internal-project', scope_id: 'wf',
      surface: 'chat', chat_context: '', created_at: '2026-10-01T00:00:00Z', browser_control_status: 'inactive',
      workflow_context: { workflow_id: 'wf', major_version: 1, initial_subversion: 7, target: { kind: 'workflow' } } }];
    const view = mount();
    fireEvent.click(screen.getByRole('button', { name: 'Choose history' }));
    expect(screen.getByRole('dialog')).toHaveAccessibleName('[v1.sv7] Global workflow');
    expect(mocks.history).toHaveBeenLastCalledWith('wf', 'history', true);
    view.unmount();
    mount();
    expect(screen.getByRole('dialog')).toHaveAccessibleName('[v1.sv7] Global workflow');
    expect(mocks.create).not.toHaveBeenCalled();
    await waitFor(() => expect(mocks.reconcile).toHaveBeenCalledWith({ wfId: 'wf', chatId: 'history', surface: 'chat' }));
  });

  it('does not start contextual conversation on a read-only canvas', () => {
    mount(true);
    fireEvent.click(screen.getByText('Node context'));
    expect(screen.queryByText('Send contextual message')).not.toBeInTheDocument();
    expect(mocks.create).not.toHaveBeenCalled();
  });
});
