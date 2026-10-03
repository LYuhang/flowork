import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { CanvasChatContext, type OpenCanvasChat, type CanvasChatPoint as Point } from './CanvasChatContext';
import { MessageSquare, WandSparkles, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { ChatComposer } from '@/components/agent-sidebar/ChatComposer';
import { ChatHistoryMenu } from '@/components/agent-sidebar/ChatHistoryMenu';
import { ChatMessageList } from '@/components/agent-sidebar/ChatMessageList';
import { SSEStatusBanner } from '@/components/agent-sidebar/SSEStatusBanner';
import { useChatSessions, useChatWorkspace, useCreateChatSession, type ChatListItem } from '@/lib/api/queries/chats';
import { useCommitWorkflow } from '@/lib/api/mutations/workflow-ops';
import { useConversationHistory } from '@/lib/chat/use-conversation-history';
import { CHAT_RECONCILE_INTERVAL_MS, reconcileChatWithServer } from '@/lib/api/sse/chat-reconcile';
import { runAgentTurn } from '@/lib/api/sse/run-agent-turn';
import { chatAccountNamespace } from '@/lib/chat/state-key';
import { useAuthStore } from '@/stores/auth';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import { fileRefFromAgentPath } from '@/lib/preview/protocol';
import { standalonePreviewHref } from '@/lib/preview/standalone-preview';
import type { components } from '@/lib/api/schema';

type Binding = components['schemas']['WorkflowChatBinding'];
type Draft = { chatId: string; binding: Binding; nodeName?: string };

function rememberedChat(key: string): string | null {
  try { return window.sessionStorage.getItem(key); } catch { return null; }
}

function panelPosition(point: Point): Point {
  return { x: Math.max(8, Math.min(point.x, window.innerWidth - 396)),
    y: Math.max(8, Math.min(point.y, window.innerHeight - 436)) };
}

/** The canvas shell owns only placement and Workflow binding. Conversations
 * use the same composer, transcript, history, SSE and Runtime APIs as main Chat.
 */
export function WorkflowCanvasChat({ wfId, readOnly, children }: {
  wfId: string; readOnly: boolean; children: ReactNode;
}) {
  const account = useAuthStore(state => state.user);
  const namespace = chatAccountNamespace(account);
  return <CanvasChatScope key={`${namespace}:${wfId}`} wfId={wfId} readOnly={readOnly}
    storageKey={`flowork:workflow-chat:${namespace}:${wfId}`}>{children}</CanvasChatScope>;
}

function CanvasChatScope({ wfId, readOnly, storageKey, children }: {
  wfId: string; readOnly: boolean; storageKey: string; children: ReactNode;
}) {
  const { t } = useTranslation();
  const sessions = useChatSessions(wfId, 'chat');
  const create = useCreateChatSession();
  const [selectedId, setSelectedId] = useState<string | null>(() => rememberedChat(storageKey));
  const [draft, setDraft] = useState<Draft | null>(null);
  const [created, setCreated] = useState<ChatListItem | null>(null);
  const [open, setOpen] = useState(() => !!rememberedChat(storageKey));
  const [point, setPoint] = useState<Point>(() => panelPosition({ x: window.innerWidth - 416, y: 100 }));
  const panel = useRef<HTMLDivElement>(null);
  const preparing = useRef(false);
  const lifecycle = useRef({ mounted: true, selectedId, readOnly });
  useLayoutEffect(() => {
    lifecycle.current.selectedId = selectedId;
    lifecycle.current.readOnly = readOnly;
  }, [selectedId, readOnly]);
  useEffect(() => {
    const state = lifecycle.current;
    state.mounted = true;
    return () => { state.mounted = false; };
  }, []);
  const saved = sessions.data?.items.find(item => item.chat_id === selectedId && item.workflow_context?.workflow_id === wfId);
  const selected = saved ?? (created?.chat_id === selectedId ? created : undefined);
  const binding = selected?.workflow_context ?? draft?.binding;
  const persisted = !!selected;
  const visibleChatId = persisted || draft ? selectedId : null;
  const transcript = useConversationHistory(wfId, visibleChatId, persisted);
  const workspace = useChatWorkspace(persisted ? selectedId : null);
  const commit = useCommitWorkflow(wfId, binding?.major_version);
  const currentBaseVersion = useWorkflowEditStore(state => state.baseVersion);
  useEffect(() => {
    if (!open || !visibleChatId || !transcript.ready) return;
    const frame = requestAnimationFrame(() => panel.current?.querySelector('textarea')?.focus());
    return () => cancelAnimationFrame(frame);
  }, [open, transcript.ready, visibleChatId]);
  const remember = useCallback((id: string) => {
    try { window.sessionStorage.setItem(storageKey, id); } catch { /* DB history remains authoritative. */ }
  }, [storageKey]);

  const openAt = useCallback<OpenCanvasChat>((target, position) => {
    if (lifecycle.current.readOnly || preparing.current) return;
    const version = useWorkflowEditStore.getState().baseVersion?.match(/^v(\d+)\.sv(\d+)$/);
    if (!version) return;
    const chatId = crypto.randomUUID();
    const node = target.kind === 'node' && target.node_id ? useWorkflowEditStore.getState().draft?.[target.node_id] : null;
    const nodeName = node && typeof node === 'object' && 'node_name' in node && typeof node.node_name === 'string'
      ? node.node_name : undefined;
    setDraft({ chatId, nodeName, binding: { workflow_id: wfId, major_version: Number(version[1]),
      initial_subversion: Number(version[2]), target } });
    setCreated(null);
    setSelectedId(chatId);
    setPoint(panelPosition(position));
    setOpen(true);
  }, [wfId]);

  const dismiss = useCallback(() => {
    if (preparing.current) return;
    setOpen(false);
    if (!persisted) { setDraft(null); setSelectedId(null); }
  }, [persisted]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!(event.target instanceof Element) || panel.current?.contains(event.target)) return;
      // Composer menus and dialogs use portals; interacting with them is not
      // a canvas click. Only the canvas itself collapses the conversation.
      if (event.target.closest('.react-flow__pane, .react-flow__node, .react-flow__edge')) dismiss();
    };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [dismiss, open]);

  useEffect(() => {
    if (!persisted || !selectedId) return;
    const reconcile = () => {
      if (document.visibilityState === 'hidden') return;
      void reconcileChatWithServer({ wfId, chatId: selectedId, surface: 'chat' });
    };
    reconcile();
    const interval = window.setInterval(reconcile, CHAT_RECONCILE_INTERVAL_MS);
    window.addEventListener('focus', reconcile);
    window.addEventListener('online', reconcile);
    document.addEventListener('visibilitychange', reconcile);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener('focus', reconcile);
      window.removeEventListener('online', reconcile);
      document.removeEventListener('visibilitychange', reconcile);
    };
  }, [persisted, selectedId, wfId]);

  const selectHistory = (id: string) => {
    if (preparing.current) return;
    const item = sessions.data?.items.find(row => row.chat_id === id && row.workflow_context?.workflow_id === wfId);
    if (!item) return;
    setDraft(null); setCreated(null); setSelectedId(id); setOpen(true); remember(id);
  };
  const prepareConversation = async (): Promise<string> => {
    if (lifecycle.current.readOnly || !lifecycle.current.mounted || !selectedId) throw new Error(t('canvasChat.readOnly'));
    if (selected?.project_id) return selected.project_id;
    if (!draft || draft.chatId !== selectedId) throw new Error(t('canvasChat.unavailable'));
    preparing.current = true;
    try {
      const state = useWorkflowEditStore.getState();
      const version = state.baseVersion?.match(/^v(\d+)\.sv(\d+)$/);
      if (!version || Number(version[1]) !== draft.binding.major_version) throw new Error(t('canvasChat.contextChanged'));
      let sub = Number(version[2]);
      if (state.isDirty()) {
        if (!state.draft) throw new Error(t('canvasChat.unavailable'));
        const result = await commit.mutateAsync(state.draft);
        if (useWorkflowEditStore.getState().isDirty()) throw new Error(t('canvasChat.contextChanged'));
        sub = result.active_sv;
      }
      if (!lifecycle.current.mounted || lifecycle.current.selectedId !== selectedId || lifecycle.current.readOnly)
        throw new Error(t('canvasChat.contextChanged'));
      const item = await create.mutateAsync({ scopeId: wfId, chatId: selectedId,
        workflowContext: { ...draft.binding, initial_subversion: sub } });
      if (!item.project_id) throw new Error(t('canvasChat.unavailable'));
      if (!lifecycle.current.mounted || lifecycle.current.selectedId !== selectedId)
        throw new Error(t('canvasChat.contextChanged'));
      setCreated(item);
      setDraft(null);
      return item.project_id;
    } finally { preparing.current = false; }
  };

  const target = binding?.target;
  const version = binding ? `v${binding.major_version}.sv${binding.initial_subversion}` : '';
  const targetLabel = target?.kind === 'node' ? `${t('canvasChat.node')} ${draft?.nodeName || target.node_id}`
    : target?.kind === 'edge' ? `${t('canvasChat.edge')} ${target.source} → ${target.target}`
      : t('canvasChat.global');
  const sameMajor = currentBaseVersion?.startsWith(`v${binding?.major_version}.sv`);
  const title = selected?.chat_context || (binding ? `[${version}] ${targetLabel}` : t('chat_history', 'Chat History'));
  const disabledReason = readOnly ? t('canvasChat.readOnly')
    : draft && !sameMajor ? t('canvasChat.contextChanged') : null;
  const openFile = (path: string) => {
    const ref = fileRefFromAgentPath(path, { projectId: selected?.project_id ?? undefined, runId: wfId });
    if (ref) window.open(standalonePreviewHref(ref, 'auto', selectedId ? { chatId: selectedId } : null), '_blank', 'noopener,noreferrer');
  };

  return <CanvasChatContext.Provider value={openAt}>
    {children}
    {!open && <div className="absolute bottom-4 right-4 z-20 flex items-center rounded-full border bg-background p-1 shadow-sm" data-role="canvas-chat-launcher">
      <Button variant="ghost" size="icon" aria-label={t('canvasChat.open')} onClick={() => setOpen(true)}><MessageSquare className="h-4 w-4" /></Button>
      <ChatHistoryMenu wfId={wfId} activeChatId={selectedId} onSelect={selectHistory} workflowContextOnly />
    </div>}
    <div ref={panel} hidden={!open} role="dialog" aria-label={title} data-role="canvas-chat"
      onKeyDown={event => { if (event.key === 'Escape' && !event.defaultPrevented) { event.stopPropagation(); dismiss(); } }}
      className="fixed z-40 flex min-h-[320px] min-w-[300px] resize flex-col overflow-hidden rounded-xl border bg-background shadow-xl"
      style={{ left: point.x, top: point.y, width: 380, height: 420,
        maxWidth: 'calc(100vw - 16px)', maxHeight: 'calc(100vh - 16px)', display: open ? undefined : 'none' }}>
      <div className="flex h-10 shrink-0 cursor-move items-center gap-2 border-b px-2"
        onPointerDown={event => {
          if (event.button !== 0 || (event.target as Element).closest('button')) return;
          const start = { x: event.clientX - point.x, y: event.clientY - point.y };
          const element = event.currentTarget;
          element.setPointerCapture(event.pointerId);
          const move = (next: PointerEvent) => setPoint(panelPosition({ x: next.clientX - start.x, y: next.clientY - start.y }));
          const done = () => { element.removeEventListener('pointermove', move); element.removeEventListener('lostpointercapture', done); };
          element.addEventListener('pointermove', move); element.addEventListener('lostpointercapture', done);
        }}>
        <WandSparkles className="h-4 w-4 shrink-0" />
        <span className="min-w-0 flex-1 truncate text-xs font-medium" title={title}>{title}</span>
        <ChatHistoryMenu wfId={wfId} activeChatId={selectedId} onSelect={selectHistory} active={open} workflowContextOnly />
        <Button variant="ghost" size="icon" className="h-7 w-7" aria-label={t('close', 'Close')} onClick={dismiss}><X className="h-4 w-4" /></Button>
      </div>
      {visibleChatId ? <>
        {!readOnly && <SSEStatusBanner wfId={wfId} activeChatId={visibleChatId} />}
        <ChatMessageList wfId={wfId} vfsScopeId={workspace.data?.workspace_scope_id}
          activeChatId={visibleChatId} compact historyItems={transcript.items}
          historyLoading={transcript.query.isLoading} historyError={transcript.query.isError}
          hasOlderHistory={transcript.hasOlder} olderHistoryLoading={transcript.loadingOlder}
          onLoadOlderHistory={transcript.loadOlder} onOpenFilePreview={openFile}
          onSubmitInteractiveAsNewMessage={async (content, control) => {
            if (readOnly || !transcript.ready) throw new Error(t('canvasChat.readOnly'));
            const projectId = await prepareConversation();
            const accepted = await runAgentTurn({ wfId, chatId: visibleChatId, projectId, content, control, agentSurface: 'chat', surface: 'main' });
            if (!accepted) throw new Error(t('canvasChat.unavailable'));
          }} />
        <div className="shrink-0 border-t">
          <ChatComposer key={visibleChatId} wfId={wfId} chatId={visibleChatId}
            projectId={selected?.project_id} chatPersisted={persisted} quietFrame showModelSelector
            historyReady={transcript.ready} disabledReason={disabledReason} prepareConversation={prepareConversation}
            onSendAccepted={() => remember(visibleChatId)} />
        </div>
      </> : <div className="flex flex-1 items-center justify-center px-6 text-center text-sm text-muted-foreground">
        {sessions.isLoading ? t('loading', 'Loading…') : selectedId ? t('canvasChat.unavailable') : t('canvasChat.empty')}
      </div>}
    </div>
  </CanvasChatContext.Provider>;
}
