/**
 * Browser-extension conversation surface, mounted by EmbedShell.
 * The carrier scope selects history; each Chat owns an automatically created
 * Project with persistent files and a Project-scoped Runtime.
 *
 * Internally:
 *   - `collapsed` toggles between a fixed-position icon button (a la
 *     Cursor / Linear) and the full 400-px aside.
 *   - `activeChatId` is the currently selected session. The header
 *     "New Chat" updates the selection for this Chat surface only;
 *     the backend saves the Chat and working directory before it is selected.
 *   - The history affordance lives in the header (`ChatHistoryMenu`); the
 *     body is the full-width conversation (`ChatMessageList`) over a
 *     bottom-pinned `ChatComposer`.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ExternalLink, FileText, MessageSquare, Plus, Settings2, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { PaneResizeHandle } from '@/components/ui/pane-resize-handle';
import { usePersistedPaneWidth } from '@/components/ui/use-persisted-pane-width';
import { StatusDot } from '@/components/ui/status';
import { useUIStore } from '@/stores/ui';
import { useChatStreamStore } from '@/stores/chat-stream';
import { resumeActiveTurn } from '@/lib/api/sse/resume-turn';
import { readServerActiveTurns } from '@/lib/api/sse/server-active-turn';
import {
  CHAT_RECONCILE_INTERVAL_MS,
  reconcileChatWithServer,
} from '@/lib/api/sse/chat-reconcile';
import { runAgentTurn } from '@/lib/api/sse/run-agent-turn';
import { useWorkflow } from '@/lib/api/queries/workflow';
import { getBasePath } from '@/lib/base-path';
import { SSEStatusBanner } from '@/components/agent-sidebar/SSEStatusBanner';
import { ChatHistoryMenu } from '@/components/agent-sidebar/ChatHistoryMenu';
import { ChatMessageList } from '@/components/agent-sidebar/ChatMessageList';
import type { SubmitInteractiveAsNewTurn } from '@/components/agent-sidebar/tool-render/InteractiveArtifactBlock';
import { ChatComposer } from '@/components/agent-sidebar/ChatComposer';
import { AgentSettingsModal } from '@/components/agent-sidebar/AgentSettingsModal';
import {
  CHAT_HISTORY_GC_TIME_MS,
  CHAT_HISTORY_STALE_TIME_MS,
  fetchChatHistory,
  useChatWorkspace,
  useChatSessions,
  useCreateChatSession,
} from '@/lib/api/queries/chats';
import { queryClient } from '@/app/query-client';
import { cn } from '@/lib/utils';
import { useConversationHistory } from '@/lib/chat/use-conversation-history';
import { fileRefFromAgentPath } from '@/lib/preview/protocol';
import { standalonePreviewHref } from '@/lib/preview/standalone-preview';

const AGENT_WIDTH_KEY = 'vibecanvas.agentWidth';
const MIN_AGENT_WIDTH = 320;
const MAX_AGENT_WIDTH = 760;
const DEFAULT_AGENT_WIDTH = 420;
type BrowserChatSession = {
  chat_id: string;
  project_id: string;
  browser_control_status?: 'inactive' | 'attaching' | 'attached' | 'lost';
};

export interface AgentChatSidebarProps {
  /**
   * Embedded side-panel variant: the sidebar fills its container
   * instead of being a fixed-width, collapsible, resizable right rail. The
   * header swaps the static "Agent" title for `📄 <wfName> ↗` (a deep link to
   * the workflow canvas, opened in a new tab) and there is no collapse button.
   */
  embedded?: boolean;
  /** Composer default mode — a bare message in `browser` mode drives the agent's browser. */
  defaultMode?: 'chat' | 'browser';
  /** Extension-local projection; browser/window ids never enter the Turn API. */
  browserControlChatId?: string;
  browserControlAvailableHere?: boolean;
  browserTransportConnected?: boolean | null;
  /** Chat history surface. Embedded side-panel uses browser history. */
  chatSurface?: 'chat' | 'browser';
  /** Embedded shell already has a Settings tab; keep the chat header focused. */
  showEmbeddedSettingsButton?: boolean;
  onOpenEmbeddedSettings?: () => void;
  /** Show the inline model selector in the composer footer. */
  showComposerModelSelector?: boolean;
  /** Whether the mounted sidebar is currently visible in its parent tab. */
  active?: boolean;
}

export function AgentChatSidebar({
  embedded = false,
  defaultMode,
  chatSurface = embedded ? 'browser' : 'chat',
  showEmbeddedSettingsButton = true,
  onOpenEmbeddedSettings,
  showComposerModelSelector = false,
  active = true,
}: AgentChatSidebarProps = {}) {
  const { t } = useTranslation();
  const lastWfId = useUIStore((s) => s.lastActiveWorkflowId);
  const activeChatId = useUIStore((s) => s.activeChatIds[chatSurface]);
  const setActiveChatId = useUIStore((s) => s.setActiveChatId);
  const { mutateAsync: createChatSession, isPending: creatingChat } = useCreateChatSession();
  const pendingChatCreation = useRef<Promise<void> | null>(null);
  const createAndSelectChat = useCallback(() => {
    if (!lastWfId) return Promise.resolve();
    if (pendingChatCreation.current) return pendingChatCreation.current;
    const previousChatId = useUIStore.getState().activeChatIds[chatSurface];
    const promise = createChatSession({ scopeId: lastWfId, chatId: crypto.randomUUID() }).then((chat) => {
      if (useUIStore.getState().activeChatIds[chatSurface] === previousChatId) {
        setActiveChatId(chatSurface, chat.chat_id);
      }
    });
    pendingChatCreation.current = promise;
    void promise.finally(() => {
      if (pendingChatCreation.current === promise) pendingChatCreation.current = null;
    }).catch(() => undefined);
    return promise;
  }, [chatSurface, createChatSession, lastWfId, setActiveChatId]);
  // When the right Inspector (w-[380px]) is open, the collapsed launcher must
  // sit to its LEFT (like the minimap) instead of hugging the viewport edge —
  // otherwise it overlaps the inspector. 380 + a small gap.
  const inspectorOpen = useUIStore((s) => s.inspectorOpen);
  // The agent is "running" while a turn is streaming — used to breathe the
  // collapsed launcher so the user sees activity even with the panel closed.
  const running = useChatStreamStore((s) =>
    activeChatId
      ? (s.runtimes[activeChatId]?.state ?? (s.chatId === activeChatId ? s.state : 'idle')) === 'streaming'
      : false,
  );
  // Embedded header shows the workflow name as a deep link. The query is
  // `enabled: !!wfId`, so a null id (main-app pre-open) is a cheap no-op.
  const wfQuery = useWorkflow(lastWfId ?? '');
  const wfName = wfQuery.data?.meta?.workflow_name;
  const sessions = useChatSessions(lastWfId, chatSurface);
  const sessionItems = useMemo(
    () => (sessions.data?.items as BrowserChatSession[] | undefined) ?? [],
    [sessions.data?.items],
  );
  const selectedChatIsPersisted =
    !!activeChatId &&
    sessionItems.some((s) => s.chat_id === activeChatId);
  // Side-panel browser media is persisted in the canonical Chat workspace,
  // never in the synthetic browser carrier scope (`lastWfId`). Keep the same
  // VFS identity contract as ChatPage so signed screenshots and other Agent
  // files render after streaming, history reloads, and extension reconnects.
  const workspace = useChatWorkspace(
    selectedChatIsPersisted ? activeChatId : null,
  );
  const workspaceScopeId = selectedChatIsPersisted
    ? (workspace.data?.workspace_scope_id ?? '')
    : '';
  const selectedSession = activeChatId
    ? sessionItems.find((s) => s.chat_id === activeChatId)
    : undefined;
  const openFilePreview = useCallback((path: string) => {
    const fileRef = fileRefFromAgentPath(path, { projectId: workspace.data?.project_id });
    if (!fileRef) return;
    window.open(
      standalonePreviewHref(fileRef, 'auto', activeChatId ? { chatId: activeChatId } : null),
      '_blank',
      'noopener,noreferrer',
    );
  }, [workspace.data?.project_id, activeChatId]);
  const {
    query: selectedHistory, items: historyItems, hasOlder: hasOlderHistory,
    loadingOlder: olderHistoryLoading, loadOlder: loadOlderHistory,
  } = useConversationHistory(lastWfId ?? '', activeChatId, selectedChatIsPersisted);
  const historyReady =
    !selectedChatIsPersisted ||
      selectedHistory.data !== undefined ||
      selectedHistory.isError;
  const [collapsed, setCollapsed] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [activeRunDiscoveryStatus, setActiveRunDiscoveryStatus] = useState<
    'pending' | 'ready' | 'error'
  >('pending');
  const { width, setWidth, resetWidth } = usePersistedPaneWidth({
    storageKey: AGENT_WIDTH_KEY,
    defaultWidth: DEFAULT_AGENT_WIDTH,
    minWidth: MIN_AGENT_WIDTH,
    maxWidth: MAX_AGENT_WIDTH,
  });

  const reconcileRef = useRef(0);
  useEffect(() => {
    if (!lastWfId) return;
    const reconcile = () => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
      const now = Date.now();
      if (now - reconcileRef.current < 2000) return;
      reconcileRef.current = now;
      void reconcileChatWithServer({
        wfId: lastWfId,
        chatId: activeChatId,
        surface: chatSurface,
      });
    };
    const onVisibility = () => {
      if (document.visibilityState === 'visible') reconcile();
    };
    const interval = window.setInterval(reconcile, CHAT_RECONCILE_INTERVAL_MS);
    window.addEventListener('online', reconcile);
    window.addEventListener('focus', reconcile);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener('online', reconcile);
      window.removeEventListener('focus', reconcile);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [activeChatId, chatSurface, lastWfId]);

  // RESUME an in-flight turn on re-entry: if a turn was running when the user
  // left (persisted via active-turn), reopen THAT chat and re-subscribe — the
  // replayed frames rebuild the streaming text + tool states (breakpoint resume).
  // Runs once; falls through to the fresh-chat default when nothing to resume.
  const discoveredWfRef = useRef<string | null>(null);
  useEffect(() => {
    if (!lastWfId) {
      discoveredWfRef.current = null;
      queueMicrotask(() => setActiveRunDiscoveryStatus('pending'));
      return;
    }
    if (discoveredWfRef.current === lastWfId) return;
    discoveredWfRef.current = lastWfId;
    if (activeChatId) {
      queueMicrotask(() => setActiveRunDiscoveryStatus('ready'));
      return;
    }
    let disposed = false;
    void (async () => {
      setActiveRunDiscoveryStatus('pending');
      const discovered = await readServerActiveTurns(lastWfId);
      // New Chat/history selection is a synchronous user decision. Do not let
      // an older startup request replace it after the network response lands.
      if (
        disposed
        || useUIStore.getState().activeChatIds[chatSurface] !== null
      ) return;
      if (discovered === null) {
        setActiveRunDiscoveryStatus('error');
        return;
      }
      const turns = discovered;
      const at = turns[turns.length - 1];
      if (at) {
        setActiveChatId(chatSurface, at.chatId);
        for (const turn of turns) void resumeActiveTurn(turn);
      }
      setActiveRunDiscoveryStatus('ready');
    })();
    return () => {
      disposed = true;
    };
  }, [activeChatId, chatSurface, lastWfId, setActiveChatId]);

  // Restore the latest persisted Sidepanel Chat even when it has no active Run:
  // post-tool Continue gates outlive their originating Turn, so active-run
  // discovery alone cannot find them. Persist the first empty Chat before
  // enabling the composer, without starting its Project sandbox.
  const initialCreationScope = useRef<string | null>(null);
  useEffect(() => {
    if (lastWfId && !activeChatId && activeRunDiscoveryStatus === 'ready') {
      const sessionsReady =
        sessions.data !== undefined || sessions.isFetched || sessions.isError;
      if (embedded && !sessionsReady) return;
      if (sessionItems[0]?.chat_id) {
        setActiveChatId(chatSurface, sessionItems[0].chat_id);
      } else if (initialCreationScope.current !== lastWfId) {
        initialCreationScope.current = lastWfId;
        void createAndSelectChat().catch(() => {
          toast.error(t('chat.createFailed', 'Could not create chat. Please try again.'));
        });
      }
    }
  }, [
    activeChatId,
    activeRunDiscoveryStatus,
    chatSurface,
    createAndSelectChat,
    embedded,
    lastWfId,
    sessionItems,
    sessions.data,
    sessions.isError,
    sessions.isFetched,
    setActiveChatId,
    t,
  ]);

  // No workflow opened yet → render nothing. The store is set by
  // `CanvasPage` on mount (and by `EmbedChatPage` for the embed), so this only
  // suppresses the sidebar on `/workspace` and `/settings`.
  if (!lastWfId) return null;

  const submitInteractiveAsNewMessage: SubmitInteractiveAsNewTurn = async (content, control) => {
    if (!activeChatId) {
      throw new Error('Continue is unavailable because no active conversation exists');
    }
    const mode = !control && defaultMode === 'browser' ? 'browser' : undefined;
    await new Promise<void>((resolve, reject) => {
      let accepted = false;
      void runAgentTurn({
        wfId: lastWfId,
        chatId: activeChatId,
        content,
        control,
        mode,
        surface: embedded ? 'sidepanel' : 'main',
        agentSurface: embedded ? 'browser' : 'chat',
        onAccepted: () => {
          accepted = true;
          resolve();
        },
      }).then(() => {
        if (!accepted) reject(new Error('Continue Turn was not accepted by the backend'));
      });
    });
  };
  const composerDisabledReason =
    activeRunDiscoveryStatus === 'error'
        ? t('composer.active_run_discovery_failed', 'Could not check active agent state. Refresh or retry in a moment.')
        : null;

  // Embedded: never collapse to a launcher — the side panel IS the chat.
  if (!embedded && collapsed) {
    // Collapsed → a small launcher in the canvas BOTTOM-RIGHT corner, sitting
    // ABOVE the React Flow minimap (≈150px tall + ~15px margin, bottom-right)
    // and near the right edge. Moved here from the old top-right (top-28): that
    // collided with the Inspector's collapse ">" button when the inspector was
    // open. `bottom-44` (176px) clears the minimap. While a turn streams it
    // breathes (the same calm blue halo as a running node) so activity is
    // visible with the panel closed.
    return (
      <Button
        variant="outline"
        size="icon"
        aria-label={t('agentSidebar.open', 'Open agent sidebar')}
        title={running ? t('agent_running', 'Agent is running…') : t('agent', 'Agent')}
        data-action="agent-sidebar-expand"
        data-agent-running={running || undefined}
        onClick={() => setCollapsed(false)}
        style={{ right: inspectorOpen ? 392 : 12 }}
        className={`fixed bottom-44 z-30 shadow${running ? ' animate-node-breathe' : ''}`}
      >
        <MessageSquare className="h-4 w-4" />
      </Button>
    );
  }

  const handleNewChat = () => {
    void createAndSelectChat().catch(() => {
      toast.error(t('chat.createFailed', 'Could not create chat. Please try again.'));
    });
  };
  const handleHistorySelect = (chatId: string) => {
    if (!lastWfId || chatId === activeChatId) return;
    // Selection is synchronous; the transcript owns its own compact loading
    // region. Never keep the whole conversation behind a menu spinner.
    setActiveChatId(chatSurface, chatId);
    prefetchHistory(chatId);
  };

  const prefetchHistory = (chatId: string) => {
    if (!lastWfId || chatId === activeChatId) return;
    void queryClient.prefetchQuery({
      queryKey: ['chat-history', lastWfId, chatId, null],
      queryFn: () => fetchChatHistory(lastWfId, chatId),
      staleTime: CHAT_HISTORY_STALE_TIME_MS,
      gcTime: CHAT_HISTORY_GC_TIME_MS,
    }).catch(() => undefined);
  };

  // Header title: browser embed is a compact browsing assistant. Other embedded
  // uses keep the workflow deep link; main app uses the static "Agent" label.
  const headerTitle = embedded ? (
    chatSurface === 'browser' ? (
      <div className="mr-auto flex min-w-0 items-center gap-2">
        <div className="relative flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-focus/10">
          <MessageSquare className="h-4 w-4 text-muted-foreground" />
          <StatusDot
            className="absolute -right-0.5 -top-0.5"
            status={running ? 'running' : 'neutral'}
            pulse={running}
          />
        </div>
        <div className="min-w-0">
          <div className="truncate text-[13px] font-semibold leading-4">{t('embed.browser.title', 'Browser assistant')}</div>

        </div>
      </div>
    ) : (
      <a
        href={`${getBasePath()}/workflow/${lastWfId}`}
        target="_blank"
        rel="noreferrer"
        className="mr-auto flex min-w-0 items-center gap-1 text-sm font-semibold hover:underline"
        data-action="agent-sidebar-open-workflow"
        title={t('agent_open_workflow', 'Open workflow')}
      >
        <FileText className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className="truncate">{wfName || lastWfId}</span>
        <ExternalLink className="h-3 w-3 shrink-0 text-muted-foreground" />
      </a>
    )
  ) : (
    <div className="mr-auto text-sm font-semibold">{t('agent', 'Agent')}</div>
  );

  return (
    <aside
      className={
        embedded
          ? // Fill the panel as a flex CHILD (flex-1 + min-h-0), not via h-full —
            // a percentage height doesn't resolve against a flex-grown parent, which
            // collapsed the aside and floated the composer to the top.
            'relative flex min-h-0 w-full flex-1 flex-col bg-surface-work'
          : 'relative flex shrink-0 flex-col border-l border-edge-structural bg-surface-work'
      }
      style={embedded ? undefined : { width }}
      data-agent-sidebar
      data-embedded={embedded || undefined}
    >
      {/* Resize handle on the left edge — main app only (the embed fills its frame). */}
      {!embedded && (
        <PaneResizeHandle
          side="left"
          width={width}
          minWidth={MIN_AGENT_WIDTH}
          maxWidth={MAX_AGENT_WIDTH}
          onWidthChange={setWidth}
          onReset={resetWidth}
          label={t('agent_resize', 'Resize agent panel')}
          dataAction="agent-sidebar-resize"
        />
      )}
      <div
        className={cn(
          'flex shrink-0 items-center',
          embedded
            ? 'min-h-16 gap-0.5 px-3 py-2'
            : 'h-12 gap-1 border-b px-3',
        )}
      >
        {headerTitle}
        <Button
          variant="ghost"
          size="icon"
          aria-label={t('new_chat', 'New Chat')}
          data-action="agent-sidebar-new-chat"
          onClick={handleNewChat}
          disabled={!lastWfId || creatingChat}
        >
          <Plus className="h-4 w-4" />
        </Button>
        <ChatHistoryMenu
          wfId={lastWfId}
          activeChatId={activeChatId}
          onSelect={handleHistorySelect}
          onIntent={prefetchHistory}
          surface={chatSurface}
          active={active}
        />
        {(!embedded || showEmbeddedSettingsButton) && (
          <Button
            variant="ghost"
            size="icon"
            aria-label={t('agent_settings.title', 'Agent settings')}
            title={t('agent_settings.title', 'Agent settings')}
            data-action="agent-sidebar-settings"
            onClick={() => onOpenEmbeddedSettings ? onOpenEmbeddedSettings() : setSettingsOpen(true)}
          >
            <Settings2 className="h-4 w-4" />
          </Button>
        )}
        {!embedded && (
          <Button
            variant="ghost"
            size="icon"
            aria-label={t('agentSidebar.close', 'Close agent sidebar')}
            data-action="agent-sidebar-collapse"
            onClick={() => setCollapsed(true)}
          >
            <X className="h-4 w-4" />
          </Button>
        )}
      </div>

      {/* SSE reconnect banner — visible on `'interrupted'` / `'failed'`
          state. Mounted above the body so it doesn't push the conversation;
          needs `wfId` for its Retry button to call `runAgentTurn` against
          the right workflow. */}
      <SSEStatusBanner wfId={lastWfId} activeChatId={activeChatId} />

      <div className="flex min-h-0 flex-1 flex-col">
        <ChatMessageList
          wfId={lastWfId}
          vfsScopeId={workspaceScopeId || undefined}
          activeChatId={activeChatId}
          surface={chatSurface}
          compact={embedded}
          historyItems={historyItems}
          historyLoading={selectedHistory.isLoading}
          hasOlderHistory={hasOlderHistory}
          olderHistoryLoading={olderHistoryLoading}
          onLoadOlderHistory={loadOlderHistory}
          onOpenFilePreview={openFilePreview}
          onSubmitInteractiveAsNewMessage={submitInteractiveAsNewMessage}
        />
        <div className={cn('relative flex-none', embedded && 'bg-surface-work before:pointer-events-none before:absolute before:-top-5 before:inset-x-0 before:h-5 before:bg-gradient-to-t before:from-surface-work before:to-transparent')}>
          <ChatComposer
            wfId={lastWfId}
            chatId={activeChatId}
            projectId={selectedSession?.project_id}
            chatPersisted={selectedChatIsPersisted}
            defaultMode={defaultMode}
            embedded={embedded}
            agentSurface={embedded ? 'browser' : 'chat'}
            historyReady={historyReady}
            showModelSelector={showComposerModelSelector}
            disabledReason={composerDisabledReason}
          />
        </div>
      </div>

      {(!embedded || (showEmbeddedSettingsButton && !onOpenEmbeddedSettings)) && (
        <AgentSettingsModal open={settingsOpen} onOpenChange={setSettingsOpen} />
      )}
    </aside>
  );
}
