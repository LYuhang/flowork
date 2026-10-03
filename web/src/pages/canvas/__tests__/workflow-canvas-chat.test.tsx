import { createPortal } from 'react-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { WorkflowCanvasChat } from '../WorkflowCanvasChat';
import { useOpenCanvasChat, useCanvasChatControls, useCanvasChatReference } from '../CanvasChatContext';
import { useChatStreamStore } from '@/stores/chat-stream';
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
  <><button onClick={() => onSelect('history')}>Choose history</button>
    {createPortal(<div role="menuitem" onClick={() => onSelect('history')}>Portalled history item</div>, document.body)}</> }));

function Launcher() {
  const open = useOpenCanvasChat();
  const controls = useCanvasChatControls();
  const reference = useCanvasChatReference();
  return <>
    {controls}
    <button disabled={!reference?.enabled} onClick={() => void reference?.add({ kind: 'node', node_id: 'code' })}>Reference node</button>
    <button disabled={!reference?.enabled} onClick={() => void reference?.add({ kind: 'workflow' })}>Reference workflow</button>
    <button onClick={() => open?.({ kind: 'node', node_id: 'code' }, { x: 50, y: 80 })}>Node context</button>
    <button onClick={() => open?.({ kind: 'edge', source: 'code', target: 'end' }, { x: 50, y: 80 })}>Edge context</button>
    <div className="react-flow__pane" data-testid="canvas" />
  </>;
}
function mount(readOnly = false) {
  return render(<WorkflowCanvasChat wfId="wf" readOnly={readOnly}><Launcher /></WorkflowCanvasChat>);
}
beforeEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  useChatStreamStore.setState({ pendingAttachments: {} });
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
  it('names a draft edge with readable endpoints and disambiguates duplicate names', () => {
    useWorkflowEditStore.getState().setDraft({
      code: { node_id: 'code', node_type: 'CodeNode', node_name: 'Result', children: ['end'] },
      end: { node_id: 'end', node_type: 'EndNode', node_name: 'Result', children: [] },
    }, 'v2.sv3');
    mount();
    fireEvent.click(screen.getByText('Edge context'));
    expect(screen.getByRole('dialog')).toHaveAccessibleName('[v2.sv3] Edge Result (code) → Result (end)');
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it('does not capture portalled history clicks as a panel drag', () => {
    mount();
    fireEvent.click(screen.getByText('Node context'));
    const heading = screen.getByRole('dialog').firstElementChild as HTMLElement;
    const capture = vi.fn();
    Object.defineProperty(heading, 'setPointerCapture', { value: capture });
    const pointerDown = () => {
      const event = new Event('pointerdown', { bubbles: true });
      Object.defineProperties(event, { button: { value: 0 }, pointerId: { value: 1 } });
      return event;
    };
    fireEvent(screen.getAllByRole('menuitem').at(-1)!, pointerDown());
    expect(capture).not.toHaveBeenCalled();
    fireEvent(heading, pointerDown());
    expect(capture).toHaveBeenCalledWith(1);
  });

  it('only references into an open conversation, deduplicates and retains the pinned version', async () => {
    mount();
    expect(screen.getByText('Reference node')).toBeDisabled();
    fireEvent.click(screen.getByText('Node context'));
    fireEvent.click(screen.getByText('Reference node'));
    const attachments = () => Object.values(useChatStreamStore.getState().pendingAttachments).flat();
    await waitFor(() => expect(attachments()).toHaveLength(1));
    expect(attachments()[0]).toMatchObject({ type: 'resource', resource: { kind: 'workflow', workflow_id: 'wf', version: 'v2.sv3' },
      selector: { kind: 'workflow_elements', node_ids: ['code'], edges: [] } });
    fireEvent.click(screen.getByText('Reference node'));
    expect(attachments()).toHaveLength(1);
    fireEvent.click(screen.getByText('Reference workflow'));
    await waitFor(() => expect(attachments()).toHaveLength(2));
    expect(attachments()[1]).not.toHaveProperty('selector');
    expect(mocks.create).not.toHaveBeenCalled();
    fireEvent.contextMenu(screen.getByTestId('canvas'));
    expect(screen.getByRole('dialog')).toBeVisible();
    const menu = document.createElement('div');
    menu.dataset.role = 'canvas-context-menu'; menu.dataset.state = 'open'; document.body.append(menu);
    fireEvent.click(screen.getByTestId('canvas'));
    expect(screen.getByRole('dialog')).toBeVisible();
    menu.remove();
    fireEvent.click(screen.getByTestId('canvas'));
    expect(screen.getByText('Reference node')).toBeDisabled();
  });

  it('saves dirty referenced objects and adds nothing on a save conflict', async () => {
    useWorkflowEditStore.getState().applyEdit(wf => ({ ...wf, code: { node_id: 'code', node_type: 'CodeNode', children: [], node_name: 'Edited' } }));
    mocks.commit.mockRejectedValueOnce(new Error('version conflict'));
    mount();
    fireEvent.click(screen.getByText('Node context'));
    fireEvent.click(screen.getByText('Reference node'));
    await waitFor(() => expect(mocks.commit).toHaveBeenCalledOnce());
    await waitFor(() => expect(screen.getByText('Reference node')).toBeEnabled());
    expect(Object.values(useChatStreamStore.getState().pendingAttachments).flat()).toHaveLength(0);
    fireEvent.click(screen.getByText('Reference node'));
    await waitFor(() => expect(Object.values(useChatStreamStore.getState().pendingAttachments).flat()).toHaveLength(1));
    expect(Object.values(useChatStreamStore.getState().pendingAttachments).flat()[0]).toMatchObject({ resource: { version: 'v2.sv4' } });
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it('starts a global conversation from the toolbar before any history exists', () => {
    mount();
    fireEvent.click(screen.getByRole('button', { name: 'New workflow conversation' }));
    expect(screen.getByRole('dialog')).toHaveAccessibleName('[v2.sv3] Global workflow');
    expect(screen.getByText('Send contextual message')).toBeEnabled();
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it('opens locally, dismisses an unsent conversation and never creates server resources', () => {
    mount();
    fireEvent.click(screen.getByText('Node context'));
    expect(screen.getByRole('dialog')).toHaveAccessibleName('[v2.sv3] Node code');
    expect(mocks.create).not.toHaveBeenCalled();
    expect(mocks.reconcile).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('canvas'));
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
    fireEvent.click(screen.getByTestId('canvas'));
    fireEvent.click(screen.getByRole('button', { name: 'New workflow conversation' }));
    expect(screen.getByRole('dialog')).toHaveAccessibleName('[v2.sv4] Global workflow');
    expect(mocks.history).toHaveBeenLastCalledWith('wf', expect.any(String), false);
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
    fireEvent.click(screen.getAllByRole('button', { name: 'Choose history' })[0]);
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
    expect(screen.queryByRole('button', { name: 'New workflow conversation' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Node context'));
    expect(screen.queryByText('Send contextual message')).not.toBeInTheDocument();
    expect(mocks.create).not.toHaveBeenCalled();
  });
});
