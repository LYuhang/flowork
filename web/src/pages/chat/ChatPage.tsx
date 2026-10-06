import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router';
import { openProjectChatDraft } from '@/lib/chat/project-draft';
import {
  Group as ResizableGroup,
  Panel as ResizablePanel,
  Separator as ResizableSeparator,
  type Layout as ResizableLayout,
} from 'react-resizable-panels';
import {
  AlertTriangle,
  Bug,
  CheckCircle2,
  Copy,
  Eye,
  FolderPlus,
  ListChecks,
  MoreHorizontal,
  PanelRightClose,
  PanelRightOpen,
  Plus,
  RefreshCw,
  Sparkles,
  X,
} from 'lucide-react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ChatShareDialog } from '@/components/agent-sidebar/ChatShareDialog';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { AsyncState } from '@/components/ui/async-state';
import { PaneResizeHandle } from '@/components/ui/pane-resize-handle';
import { usePersistedPaneWidth } from '@/components/ui/use-persisted-pane-width';
import { StatusDot, type SemanticStatus } from '@/components/ui/status';
import { ChatMessageList } from '@/components/agent-sidebar/ChatMessageList';
import type { SubmitInteractiveAsNewTurn } from '@/components/agent-sidebar/tool-render/InteractiveArtifactBlock';
import {
  interactiveArtifactRenderError,
  readInteractiveArtifact,
} from '@/components/agent-sidebar/tool-render/interactive-artifact-contract';
import {
  diagramPreviewPathFromStandardResult,
  parseStandardToolResult,
} from '@/components/agent-sidebar/tool-render/parseStandardToolResult';
import { mergeChunks, type MergedToolCall, type RawChunk } from '@/components/agent-sidebar/types';
import { workflowIdFromToolCall } from '@/components/agent-sidebar/tool-call-utils';
import { ChatComposer } from '@/components/agent-sidebar/ChatComposer';
import { SSEStatusBanner } from '@/components/agent-sidebar/SSEStatusBanner';
import { VfsFilesSection } from '@/pages/canvas/explorer/VfsFilesSection';
import { ChatTodoDock } from './ChatTodoDock';
import { EmptyChatExamples } from './EmptyChatExamples';
import {
  CHAT_PANE_MIN_WIDTH,
  DEBUG_PANE_MIN_WIDTH,
  PREVIEW_PANE_MIN_WIDTH,
  WORKFLOW_PREVIEW_PANE_MIN_WIDTH,
  loadChatPaneLayout,
  saveChatPaneLayout,
} from './chatPaneLayout';
import { useAuthStore } from '@/stores/auth';
import { useChatStreamStore } from '@/stores/chat-stream';
import { useUIStore } from '@/stores/ui';
import {
  CHAT_INITIAL_HISTORY_LIMIT,
  fetchChatHistoryPage,
  useChatHistory,
  useChatProjects,
  useCreateChatProject,
  useChatSessions,
  useChatWorkspace,
  useChatState,
  useProjectWorkspace,
  useGeneralChatBootstrap,
  type ChatListItem,
} from '@/lib/api/queries/chats';
import { resumeActiveTurn } from '@/lib/api/sse/resume-turn';
import { readServerActiveTurns } from '@/lib/api/sse/server-active-turn';
import {
  CHAT_RECONCILE_INTERVAL_MS,
  reconcileChatWithServer,
} from '@/lib/api/sse/chat-reconcile';
import { runAgentTurn } from '@/lib/api/sse/run-agent-turn';
import { ChatDebugPanel } from './ChatDebugPanel';
import { cn } from '@/lib/utils';
import {
  EMPTY_CHAT_VIEW_STATE,
  filePreviewItem,
  previewItemToRestore,
  readChatViewPreferences,
  type ChatPreviewItem,
  writeChatViewPreferences,
} from '@/lib/chat/preview-state';
import { fileRefFromAgentPath } from '@/lib/preview/protocol';
import {
  chatAccountNamespace,
  chatClientStateKey,
  readRecentChatLocation,
  writeRecentChatSelection,
} from '@/lib/chat/state-key';
import { mergeHistoryWindow, type ChatHistoryWindow } from './history-window';

const ChatPreviewPane = lazy(() =>
  import('./ChatPreviewPane').then((module) => ({ default: module.ChatPreviewPane })),
);

const EMPTY_RAW_CHUNKS: RawChunk[] = [];
const MAX_HISTORY_WINDOWS = 20;
const MAX_STARTED_CHAT_IDS = 50;

function retainHistoryWindow(
  current: Record<string, ChatHistoryWindow>,
  key: string,
  value: ChatHistoryWindow,
): Record<string, ChatHistoryWindow> {
  const next = { ...current };
  delete next[key];
  next[key] = value;
  return Object.fromEntries(Object.entries(next).slice(-MAX_HISTORY_WINDOWS));
}

function ChatPaneSeparator({ label }: { label: string }) {
  return (
    <ResizableSeparator
      className="group relative z-20 w-1 shrink-0 cursor-col-resize bg-border/70 outline-none transition-colors hover:bg-primary/45 focus-visible:bg-primary/45 data-[separator=active]:bg-primary/60"
      aria-label={label}
      title={label}
      data-role="chat-pane-resize-handle"
    >
      <span className="pointer-events-none absolute inset-y-0 -left-1.5 -right-1.5" />
    </ResizableSeparator>
  );
}

function ChatToolbarTooltip({
  label,
  children,
}: {
  label: string;
  children: ReactElement;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent side="bottom">{label}</TooltipContent>
    </Tooltip>
  );
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function fileNameFromPath(path: string): string {
  return path.split('/').filter(Boolean).at(-1) || path;
}

function previewItemFromToolCall(
  call: MergedToolCall,
  projectId: string | null,
): ChatPreviewItem | null {
  if (call.status === 'done') {
    const result = parseStandardToolResult(call.result);
    const path = diagramPreviewPathFromStandardResult(result);
    const fileRef = path ? fileRefFromAgentPath(path, { projectId }) : null;
    if (path && fileRef) return filePreviewItem(fileRef, fileNameFromPath(path));
  }
  const parsed = readInteractiveArtifact(call);
  const artifact = parsed.artifact;
  if (interactiveArtifactRenderError(artifact, Boolean(parsed.previewOnly))) return null;
  if (!artifact) return null;
  const artifactId = artifact.artifact_id || call.id;
  const title = artifact.title || 'Interactive artifact';
  if (artifact.component_type === 'file_preview') {
    const path = stringValue(artifact.props?.path || artifact.props?.file_path || artifact.props?.ref);
    const fileRef = path ? fileRefFromAgentPath(path, { projectId }) : null;
    if (fileRef) return filePreviewItem(fileRef, fileNameFromPath(path));
  }
  return {
    id: `interactive:${artifactId}`,
    title,
    resource: { schemaVersion: 1, kind: 'interactive', artifactId },
    artifact,
  };
}



export function ChatPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const account = useAuthStore((state) => state.user);
  const activeChatId = useUIStore((s) => s.activeChatIds.chat);
  const [searchParams] = useSearchParams();
  // Empty persisted chats are intentionally absent from conversation history.
  // A source deep link verifies their workspace independently, including reloads.
  const resumedChat = useChatWorkspace(searchParams.get('resumeChat') === activeChatId ? activeChatId : null);
  const activeProjectId = useUIStore((s) => s.activeProjectId);
  const setActiveChatId = useUIStore((s) => s.setActiveChatId);
  const setActiveProjectId = useUIStore((s) => s.setActiveProjectId);
  const chatEntryIntent = useUIStore((s) => s.chatEntryIntent);
  const setChatEntryIntent = useUIStore((s) => s.setChatEntryIntent);
  const optimisticChatSessions = useUIStore((s) => s.optimisticChatSessions);
  const chatViewKey = chatClientStateKey({
    account,
    scopeId: 'general-chat',
    surface: 'chat',
    chatId: activeChatId ?? 'draft',
    suffix: 'view',
  });
  const chatViewStorageKey = `vibecanvas:chat-view:v1:${chatViewKey}`;
  const persistedViewPreferences = useMemo(
    () => readChatViewPreferences(chatViewStorageKey),
    [chatViewStorageKey],
  );
  const storedChatViewState = useUIStore((state) => state.chatViewStates[chatViewKey]);
  const chatViewState = useMemo(
    () => storedChatViewState ?? (
      persistedViewPreferences
        ? { ...EMPTY_CHAT_VIEW_STATE, ...persistedViewPreferences }
        : EMPTY_CHAT_VIEW_STATE
    ),
    [persistedViewPreferences, storedChatViewState],
  );
  const setChatViewState = useUIStore((s) => s.setChatViewState);
  const {
    explorerOpen,
    debugOpen,
    previewOpen,
    todoCollapsed,
    previewItems,
    activePreviewId,
  } = chatViewState;
  useEffect(() => {
    if (!storedChatViewState && persistedViewPreferences) {
      setChatViewState(chatViewKey, persistedViewPreferences);
    }
  }, [chatViewKey, persistedViewPreferences, setChatViewState, storedChatViewState]);
  useEffect(() => {
    if (!account || !activeChatId) return;
    writeChatViewPreferences(chatViewStorageKey, chatViewState);
  }, [account, activeChatId, chatViewState, chatViewStorageKey]);
  const setExplorerOpen = useCallback(
    (next: boolean | ((current: boolean) => boolean)) =>
      setChatViewState(chatViewKey, (current) => ({
        explorerOpen: typeof next === 'function' ? next(current.explorerOpen) : next,
      })),
    [chatViewKey, setChatViewState],
  );
  const setDebugOpen = useCallback(
    (next: boolean | ((current: boolean) => boolean)) =>
      setChatViewState(chatViewKey, (current) => ({
        debugOpen: typeof next === 'function' ? next(current.debugOpen) : next,
      })),
    [chatViewKey, setChatViewState],
  );
  const setPreviewOpen = useCallback(
    (next: boolean | ((current: boolean) => boolean)) =>
      setChatViewState(chatViewKey, (current) => ({
        previewOpen: typeof next === 'function' ? next(current.previewOpen) : next,
      })),
    [chatViewKey, setChatViewState],
  );
  const previewToggleButtonRef = useRef<HTMLButtonElement>(null);
  const previewWasOpenRef = useRef(previewOpen);
  const focusAfterPreviewClose = useCallback(() => {
    requestAnimationFrame(() => {
      const previewToggle = previewToggleButtonRef.current;
      if (previewToggle && !previewToggle.disabled) {
        previewToggle.focus();
        return;
      }
      document.querySelector<HTMLTextAreaElement>(
        '[data-role="agent-composer-input"]:not(:disabled)',
      )?.focus();
    });
  }, []);
  const closePreviewPane = useCallback(() => {
    setPreviewOpen(false);
  }, [setPreviewOpen]);
  useEffect(() => {
    const wasOpen = previewWasOpenRef.current;
    previewWasOpenRef.current = previewOpen;
    if (wasOpen && !previewOpen) focusAfterPreviewClose();
  }, [focusAfterPreviewClose, previewOpen]);
  const setPreviewItems = useCallback(
    (next: ChatPreviewItem[] | ((current: ChatPreviewItem[]) => ChatPreviewItem[])) =>
      setChatViewState(chatViewKey, (current) => ({
        previewItems: typeof next === 'function' ? next(current.previewItems) : next,
      })),
    [chatViewKey, setChatViewState],
  );
  const setActivePreviewId = useCallback(
    (next: string | null) => setChatViewState(chatViewKey, { activePreviewId: next }),
    [chatViewKey, setChatViewState],
  );
  const setTodoCollapsed = useCallback(
    (next: boolean) => setChatViewState(chatViewKey, { todoCollapsed: next }),
    [chatViewKey, setChatViewState],
  );
  const [startedChatIds, setStartedChatIds] = useState<Set<string>>(() => new Set());
  const [sandboxSelectionKey, setSandboxSelectionKey] = useState<string | null>(null);
  const [historyWindows, setHistoryWindows] = useState<Record<string, ChatHistoryWindow>>({});
  const [chatIdCopied, setChatIdCopied] = useState(false);
  const [firstProjectName, setFirstProjectName] = useState('');
  const chatIdCopyResetTimer = useRef<number | null>(null);
  const boot = useGeneralChatBootstrap();
  const projects = useChatProjects();
  const createProject = useCreateChatProject();
  const accountNamespace = chatAccountNamespace(account);
  const restoredChatLocation = useMemo(
    () => readRecentChatLocation(account, 'chat'),
    [account],
  );
  const restoredChatId = restoredChatLocation?.chatId ?? null;
  useEffect(() => {
    if (!activeChatId && chatEntryIntent === null && restoredChatId) {
      if (restoredChatLocation?.draft && restoredChatLocation.projectId) {
        setActiveProjectId(restoredChatLocation.projectId);
      }
      setActiveChatId('chat', restoredChatId);
    }
  }, [activeChatId, chatEntryIntent, restoredChatId, restoredChatLocation, setActiveChatId, setActiveProjectId]);
  const sandboxPane = usePersistedPaneWidth({
    storageKey: `vibecanvas:chat-sandbox-pane-width:v1:${accountNamespace}`,
    defaultWidth: 320,
    minWidth: 272,
    maxWidth: 560,
  });
  // A tab-scoped opaque scope/chat hint lets the transcript request start as
  // soon as auth restores the page, rather than waiting behind bootstrap and
  // the full session inventory. Bootstrap remains authoritative and replaces
  // the hint if the server's carrier scope ever changes.
  const carrierScopeId = boot.data?.carrier_scope_id ?? restoredChatLocation?.scopeId ?? '';
  const createFirstProject = useCallback(async () => {
    const name = firstProjectName.trim().replace(/\s+/g, ' ');
    if (!name || !carrierScopeId) return;
    try {
      const project = await createProject.mutateAsync(name);
      openProjectChatDraft({ account, projectId: project.project_id, scopeId: carrierScopeId });
      setFirstProjectName('');
    } catch {
      toast.error(t('nav.projects.createFailed', 'Could not create project'));
    }
  }, [account, carrierScopeId, createProject, firstProjectName, t]);
  const [composerHasDraft, setComposerHasDraft] = useState(false);
  const composerStateKey = activeChatId
    ? chatClientStateKey({
        account,
        scopeId: carrierScopeId,
        surface: 'chat',
        chatId: activeChatId,
      })
    : null;
  const setComposerInput = useChatStreamStore((state) => state.setComposerInput);
  const fillComposerExample = useCallback((prompt: string) => {
    if (!composerStateKey) return;
    setComposerInput(composerStateKey, prompt);
    window.requestAnimationFrame(() => {
      const input = document.querySelector<HTMLTextAreaElement>(
        `[data-role="agent-composer-input"][data-chat-id="${CSS.escape(activeChatId ?? '')}"]`,
      );
      input?.focus();
      input?.setSelectionRange(prompt.length, prompt.length);
    });
  }, [activeChatId, composerStateKey, setComposerInput]);
  const openPreviewItem = useCallback((item: ChatPreviewItem) => {
    setPreviewItems((prev) => {
      const next = prev.filter((existing) => existing.id !== item.id);
      return [...next, item];
    });
    setActivePreviewId(item.id);
    setPreviewOpen(true);
  }, [setActivePreviewId, setPreviewItems, setPreviewOpen]);
  const focusedJobKey = useRef('');
  const focusJob = searchParams.get('focusJob')?.slice(0, 512);
  useEffect(() => {
    if (!activeChatId || !focusJob || searchParams.get('resumeChat') !== activeChatId || !resumedChat.data) return;
    const key = `${activeChatId}:${focusJob}`;
    if (focusedJobKey.current === key) return;
    focusedJobKey.current = key;
    openPreviewItem({
      id: `background_jobs:${activeChatId}:${focusJob}`,
      title: t('chat.background.title', 'Background tasks'),
      resource: { schemaVersion: 1, kind: 'background_jobs', chatId: activeChatId, jobId: focusJob },
    });
  }, [activeChatId, focusJob, searchParams, resumedChat.data, openPreviewItem, t]);
  const closePreviewItem = useCallback((id: string) => {
    setPreviewItems((prev) => {
      const index = prev.findIndex((item) => item.id === id);
      const next = prev.filter((item) => item.id !== id);
      if (activePreviewId === id) {
        const replacement = next[Math.min(index, next.length - 1)] ?? next[next.length - 1] ?? null;
        setActivePreviewId(replacement?.id ?? null);
        if (!replacement) closePreviewPane();
      }
      return next;
    });
  }, [activePreviewId, closePreviewPane, setActivePreviewId, setPreviewItems]);
  const selectPreviewItem = useCallback((id: string) => {
    setActivePreviewId(id);
    setPreviewOpen(true);
  }, [setActivePreviewId, setPreviewOpen]);
  const chatSessions = useChatSessions(carrierScopeId || null);
  const chatSessionItems = useMemo(() => {
    const persistedRaw = (chatSessions.data?.items ?? []) as ChatListItem[];
    if (!carrierScopeId) return persistedRaw;
    const optimisticForScope = optimisticChatSessions.filter(
      (item) => item.scopeId === carrierScopeId && item.surface === 'chat',
    );
    const optimisticById = new Map(optimisticForScope.map((item) => [item.chat_id, item]));
    const persisted = persistedRaw.map((item) => {
      const optimistic = optimisticById.get(item.chat_id);
      const persistedTitle = (item.chat_context || '').trim().toLowerCase();
      if (optimistic?.chat_context && (!persistedTitle || persistedTitle === 'new chat')) {
        return { ...item, chat_context: optimistic.chat_context };
      }
      return item;
    });
    const persistedIds = new Set(persisted.map((item) => item.chat_id));
    const optimistic: ChatListItem[] = optimisticForScope
      .filter((item) => !persistedIds.has(item.chat_id))
      .map((item) => ({
        chat_id: item.chat_id,
        project_id: item.projectId,
        scope_id: carrierScopeId,
        chat_context: item.chat_context,
        created_at: item.created_at,
      } as ChatListItem));
    return [...optimistic, ...persisted];
  }, [carrierScopeId, chatSessions.data?.items, optimisticChatSessions]);
  const activeChatSession = useMemo(
    () => chatSessionItems.find((s) => s.chat_id === activeChatId) ?? null,
    [activeChatId, chatSessionItems],
  );
  const selectedProjectId = activeChatSession?.project_id ?? activeProjectId;
  const activeProject = projects.data?.find(
    (project) => project.project_id === selectedProjectId,
  ) ?? null;
  useEffect(() => {
    if (activeChatSession?.project_id && activeChatSession.project_id !== activeProjectId) {
      setActiveProjectId(activeChatSession.project_id);
    }
  }, [activeChatSession?.project_id, activeProjectId, setActiveProjectId]);
  const reconcileRef = useRef(0);
  useEffect(() => {
    if (!carrierScopeId) return;
    const reconcile = () => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
      const now = Date.now();
      if (now - reconcileRef.current < 2000) return;
      reconcileRef.current = now;
      void reconcileChatWithServer({
        wfId: carrierScopeId,
        chatId: activeChatId,
        surface: 'chat',
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
  }, [activeChatId, carrierScopeId]);
  const activeChatIsPersisted = useMemo(
    () =>
      (resumedChat.isSuccess && resumedChat.data.chat_id === activeChatId) || ((chatSessions.data?.items ?? []) as ChatListItem[]).some(
        (s) => s.chat_id === activeChatId,
      ),
    [activeChatId, chatSessions.data?.items, resumedChat.isSuccess, resumedChat.data],
  );
  useEffect(() => {
    if (activeChatId && activeChatIsPersisted) {
      writeRecentChatSelection(account, 'chat', activeChatId, carrierScopeId);
    }
  }, [account, activeChatId, activeChatIsPersisted, carrierScopeId]);
  // The list is authorized, and explicit Chat creation commits its auth
  // projection before returning. Resource readiness does not depend on a
  // sandbox: a cold Project still exposes persisted history and files.
  const activeChatResourcesReady = activeChatIsPersisted && Boolean(activeChatId);
  const markChatStarted = useCallback((chatId: string | null | undefined) => {
    if (!chatId) return;
    setStartedChatIds((current) => {
      if (current.has(chatId)) return current;
      const next = new Set(current);
      next.add(chatId);
      return new Set([...next].slice(-MAX_STARTED_CHAT_IDS));
    });
  }, []);
  const submitInteractiveAsNewMessage: SubmitInteractiveAsNewTurn = useCallback(async (content, control) => {
    if (!carrierScopeId || !activeChatId) {
      throw new Error('Continue is unavailable because no active conversation exists');
    }
    markChatStarted(activeChatId);
    if (!control) {
      useUIStore.getState().addOptimisticChatSession({
        scopeId: carrierScopeId,
        projectId: selectedProjectId,
        chat_id: activeChatId,
        chat_context: content.slice(0, 80),
        surface: 'chat',
      });
    }
    await new Promise<void>((resolve, reject) => {
      let accepted = false;
      void runAgentTurn({
        wfId: carrierScopeId,
        chatId: activeChatId,
        projectId: selectedProjectId,
        content,
        control,
        surface: 'main',
        agentSurface: 'chat',
        onAccepted: () => {
          accepted = true;
          resolve();
        },
      }).then(() => {
        if (!accepted) reject(new Error('Continue Turn was not accepted by the backend'));
      });
    });
  }, [activeChatId, carrierScopeId, markChatStarted, selectedProjectId]);
  const activeChatStartedThisView = !!activeChatId && startedChatIds.has(activeChatId);
  const activeChatLooksEmpty = !activeChatSession ||
    !activeChatSession.chat_context ||
    activeChatSession.chat_context.trim().toLowerCase() === 'new chat' ||
    activeChatSession.chat_context.trim() === t('new_chat', 'New Chat');
  // History is the only blocking resource when a user opens an existing Chat.
  // Fetch secondary chrome (workspace, sandbox, plans, runtime choices) only
  // after the recent transcript is visible so an occasional backend spike
  // cannot make six independent requests compete with the content the user is
  // actually waiting to read.
  const activeProjectionTurnId = useChatStreamStore((state) => {
    if (!activeChatId) return null;
    const runtime = state.runtimes[activeChatId];
    return runtime?.projectionActive ? runtime.turnId : null;
  });
  const activeChatCanLoadHistory = Boolean(
    activeChatIsPersisted
    || (activeChatId && activeChatId === restoredChatId && !restoredChatLocation?.draft && !chatSessions.isError),
  );
  const activeHistory = useChatHistory(
    carrierScopeId || null,
    activeChatCanLoadHistory ? activeChatId : null,
    activeChatCanLoadHistory,
    activeProjectionTurnId || null,
  );
  const secondaryChatResourcesReady = Boolean(
    activeChatResourcesReady
    && (
      !activeChatIsPersisted
      || activeHistory.data !== undefined
      || activeHistory.isError
    )
  );
  const workspace = useProjectWorkspace(selectedProjectId);
  const initialChatSelectionRef = useRef(false);
  const activeRunDiscoveryGenerationRef = useRef(0);
  const [activeRunDiscoveryStatus, setActiveRunDiscoveryStatus] = useState<
    'pending' | 'ready' | 'error'
  >('pending');

  useEffect(() => {
    if (
      !restoredChatId
      || activeChatId !== restoredChatId
      || chatSessions.isPending
      || activeChatIsPersisted
      || (restoredChatLocation?.draft && (projects.isPending || projects.data?.some((project) => project.project_id === restoredChatLocation.projectId)))
    ) return;
    // The hint may outlive a remotely deleted Chat. Drop it after the
    // authoritative inventory arrives, then let normal startup selection pick
    // the newest valid session (or a fresh draft).
    writeRecentChatSelection(account, 'chat', null);
    initialChatSelectionRef.current = false;
    setActiveChatId('chat', null);
  }, [
    account,
    activeChatId,
    activeChatIsPersisted,
    chatSessions.isPending,
    restoredChatId,
    restoredChatLocation,
    projects.data,
    projects.isPending,
    setActiveChatId,
  ]);

  // Resume the restored conversation once per selection. The startup-selection
  // effect below also observes project/session lists, which settle separately;
  // doing discovery there issued the same request on every list update.
  useEffect(() => {
    if (!carrierScopeId || !activeChatId || activeChatId !== restoredChatId) return;
    let disposed = false;
    void readServerActiveTurns(carrierScopeId).then((turns) => {
      if (disposed) return;
      if (turns === null) {
        setActiveRunDiscoveryStatus('error');
        return;
      }
      for (const turn of turns) {
        markChatStarted(turn.chatId);
        void resumeActiveTurn(turn);
      }
      setActiveRunDiscoveryStatus('ready');
    });
    return () => { disposed = true; };
  }, [activeChatId, carrierScopeId, markChatStarted, restoredChatId]);

  useEffect(() => {
    if (!carrierScopeId) return;
    const discoveryGeneration = ++activeRunDiscoveryGenerationRef.current;
    // Explicit navigation intent is already authoritative client state. It
    // must not wait for the durable list or active-run discovery before the
    // requested shell becomes usable.
    if (chatEntryIntent === 'select') {
      initialChatSelectionRef.current = true;
      setChatEntryIntent(null);
      queueMicrotask(() => setActiveRunDiscoveryStatus('ready'));
      return;
    }
    if (chatEntryIntent === 'default') {
      if (projects.isPending || chatSessions.isPending) return;
      initialChatSelectionRef.current = true;
      const projectId = projects.data?.some(
        (project) => project.project_id === activeProjectId,
      ) ? activeProjectId : projects.data?.[0]?.project_id ?? null;
      if (!projectId) {
        setActiveProjectId(null);
        setActiveChatId('chat', null);
        setChatEntryIntent(null);
        queueMicrotask(() => setActiveRunDiscoveryStatus('ready'));
        return;
      }
      setActiveProjectId(projectId);
      setActiveChatId('chat', chatSessions.data?.items.find((chat) => chat.project_id === projectId)?.chat_id ?? null);
      setChatEntryIntent(null);
      queueMicrotask(() => setActiveRunDiscoveryStatus('ready'));
      return;
    }
    // A non-null route selection is already authoritative. Startup discovery
    // exists only to choose a destination when a hard refresh has no active
    // Chat in the in-memory store; it must never replace an id chosen by the
    // user or by an explicit navigation intent.
    if (activeChatId) {
      initialChatSelectionRef.current = true;
      if (activeChatId !== restoredChatId) {
        queueMicrotask(() => setActiveRunDiscoveryStatus('ready'));
      }
      return;
    }
    // A hard refresh resets the in-memory UI store. Wait for the durable Chat
    // list before choosing a destination; otherwise the bootstrap races the
    // sessions query, creates a fresh draft, and hides the conversation the
    // user was just viewing (including any pending Continue card).
    if (chatSessions.isPending || projects.isPending) return;
    if (initialChatSelectionRef.current) return;
    initialChatSelectionRef.current = true;
    let disposed = false;
    void (async () => {
      const discovered = await readServerActiveTurns(carrierScopeId);
      // A user may explicitly select or create a Chat while active-turn
      // discovery is in flight. That navigation intent is authoritative even
      // before React has committed the resulting activeChatId render. Without
      // this synchronous store check, the stale discovery result can win the
      // race and switch the page back to an older, connection-bound Chat.
      if (
        disposed
        || discoveryGeneration !== activeRunDiscoveryGenerationRef.current
        || useUIStore.getState().chatEntryIntent !== null
        || useUIStore.getState().activeChatIds.chat !== activeChatId
      ) return;
      if (discovered === null) {
        setActiveRunDiscoveryStatus('error');
        return;
      }
      const turns = discovered;
      const at = turns[turns.length - 1];
      if (at) {
        const runningChat = (chatSessions.data?.items ?? []).find(
          (item) => item.chat_id === at.chatId,
        ) as ChatListItem | undefined;
        if (runningChat?.project_id) setActiveProjectId(runningChat.project_id);
        setActiveChatId('chat', at.chatId);
        for (const turn of turns) markChatStarted(turn.chatId);
        for (const turn of turns) void resumeActiveTurn(turn);
        setChatEntryIntent(null);
        setActiveRunDiscoveryStatus('ready');
        return;
      }
      const latestPersistedChat = (
        (chatSessions.data?.items ?? []) as ChatListItem[]
      )[0];
      if (latestPersistedChat) {
        if (latestPersistedChat.project_id) {
          setActiveProjectId(latestPersistedChat.project_id);
        }
        setActiveChatId('chat', latestPersistedChat.chat_id);
        setChatEntryIntent(null);
        setActiveRunDiscoveryStatus('ready');
        return;
      }
      const projectId = projects.data?.some(
        (project) => project.project_id === activeProjectId,
      ) ? activeProjectId : projects.data?.[0]?.project_id ?? null;
      if (!projectId) {
        setActiveProjectId(null);
        setActiveChatId('chat', null);
        setChatEntryIntent(null);
        setActiveRunDiscoveryStatus('ready');
        return;
      }
      setActiveProjectId(projectId);
      // Viewing an empty Project is read-only. Only an explicit New Chat
      // action creates storage; refresh must not silently add conversations.
      setActiveChatId('chat', null);
      setChatEntryIntent(null);
      setActiveRunDiscoveryStatus('ready');
    })();
    return () => {
      disposed = true;
    };
  }, [
    activeChatId,
    activeProjectId,
    carrierScopeId,
    chatEntryIntent,
    chatSessions.data?.items,
    chatSessions.isPending,
    markChatStarted,
    projects.data,
    projects.isPending,
    restoredChatId,
    setActiveChatId,
    setActiveProjectId,
    setChatEntryIntent,
  ]);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (active) {
        setSandboxSelectionKey(null);
      }
    });
    return () => {
      active = false;
    };
  }, [activeChatId]);

  const workspaceScopeId = workspace.data?.workspace_scope_id ?? '';
  const mountScopeId = workspace.data?.mount_scope_id ?? '';
  // Durable history owns completed turns while the live projection owns the
  // active turn. Excluding that turn from the query prevents the same user/AI
  // messages from being rendered once from each source.
  // The visible transcript window belongs to the Chat, not to an individual
  // Turn. Starting a Turn briefly uses an empty Turn id until the POST is
  // accepted; keying this cache by that id would swap the already-rendered
  // history for an empty window and make the conversation flash away.
  const activeHistoryKey = carrierScopeId && activeChatId
    ? `${carrierScopeId}:${activeChatId}`
    : '';
  // Derive the current tail directly from the query result so the pagination
  // affordance is present in the same render as the first transcript page.
  // Local state retains only pages explicitly loaded before that tail. This
  // avoids a transient state where messages are visible but offset/hasOlder
  // still belong to the previous render and the user must refresh the page.
  const activeHistoryWindow = useMemo(() => {
    if (!activeHistoryKey) return undefined;
    const retained = historyWindows[activeHistoryKey];
    return activeHistory.data
      ? mergeHistoryWindow(retained, activeHistory.data)
      : retained;
  }, [activeHistory.data, activeHistoryKey, historyWindows]);
  const olderHistoryLoadingRef = useRef(false);
  const [olderHistoryLoading, setOlderHistoryLoading] = useState(false);
  const hasOlderHistory = !!activeHistoryWindow && activeHistoryWindow.offset > 0;
  const loadOlderHistory = useCallback(async () => {
    if (!carrierScopeId || !activeChatId || !activeHistoryKey || !activeHistoryWindow) return;
    if (olderHistoryLoadingRef.current || activeHistoryWindow.offset <= 0) return;
    olderHistoryLoadingRef.current = true;
    setOlderHistoryLoading(true);
    try {
      // Keep older-history reads incremental. The message list may request
      // multiple pages only while its viewport is still under-filled; once it
      // becomes scrollable, further pages are fetched by an explicit upward
      // scroll near the top.
      const limit = Math.min(CHAT_INITIAL_HISTORY_LIMIT, activeHistoryWindow.offset);
      const offset = Math.max(0, activeHistoryWindow.offset - limit);
      const page = await fetchChatHistoryPage(
        carrierScopeId,
        activeChatId,
        { limit, offset, beforeTurnId: activeProjectionTurnId },
      );
      setHistoryWindows((current) =>
        retainHistoryWindow(
          current,
          activeHistoryKey,
          mergeHistoryWindow(current[activeHistoryKey], page),
        ),
      );
    } finally {
      olderHistoryLoadingRef.current = false;
      setOlderHistoryLoading(false);
    }
  }, [activeChatId, activeHistoryKey, activeHistoryWindow, activeProjectionTurnId, carrierScopeId]);
  const chatState = useChatState(
    carrierScopeId || null,
    secondaryChatResourcesReady ? activeChatId : null,
    secondaryChatResourcesReady,
  );
  const streamTodoItems = useChatStreamStore((s) =>
    activeChatId
      ? (s.runtimes[activeChatId]?.todoItems ??
        (s.chatId === activeChatId ? s.todoItems : null))
      : null,
  );
  const livePreviewChunks = useChatStreamStore((s) =>
    activeChatId ? (s.runtimes[activeChatId]?.buffer ?? EMPTY_RAW_CHUNKS) : EMPTY_RAW_CHUNKS,
  );
  const todoItems = streamTodoItems ?? chatState.data?.todo_items ?? [];
  const backgroundJobs = useMemo(
    () => chatState.data?.background_jobs ?? [],
    [chatState.data?.background_jobs],
  );
  const attentionJobs = backgroundJobs.filter((job) =>
    ['failed', 'cancelling'].includes(job.status),
  );
  const attentionCount = attentionJobs.length;
  const activeStreamState = useChatStreamStore((state) => {
    if (!activeChatId) return 'idle';
    return state.runtimes[activeChatId]?.state
      ?? (state.chatId === activeChatId ? state.state : 'idle');
  });
  const activeChatTitle = activeChatLooksEmpty
    ? t('new_chat', 'New Chat')
    : activeChatSession?.chat_context.trim() || t('new_chat', 'New Chat');
  const chatStatus: { label: string; tone: SemanticStatus; pulse?: boolean } =
    attentionCount > 0
      ? { label: t('chat.status.attention', 'Needs attention'), tone: 'warning' }
      : activeStreamState === 'streaming'
        ? { label: t('chat.status.running', 'Running'), tone: 'running', pulse: true }
        : activeStreamState === 'cancelled'
          ? { label: t('chat.status.cancelled', 'Cancelled'), tone: 'warning' }
        : activeStreamState === 'failed'
          ? { label: t('chat.status.failed', 'Failed'), tone: 'danger' }
          : activeStreamState === 'interrupted'
            ? { label: t('chat.status.interrupted', 'Interrupted'), tone: 'warning' }
            : { label: t('chat.status.ready', 'Ready'), tone: 'neutral' };
  const showChatExecutionStatus = chatStatus.tone !== 'neutral';
  const terminalStatusRef = useRef<Map<string, string>>(new Map());
  const completionBaselineRef = useRef<string | null>(null);
  useEffect(() => {
    if (!activeChatId) return;
    const current = new Map<string, string>();
    for (const job of backgroundJobs) current.set(`job:${job.job_id}`, job.status);
    if (completionBaselineRef.current !== activeChatId) {
      completionBaselineRef.current = activeChatId;
      terminalStatusRef.current = current;
      return;
    }
    for (const [key, status] of current) {
      const previous = terminalStatusRef.current.get(key);
      if (!previous || previous === status) continue;
      if (status === 'completed') {
        toast.success(t('chat.notifications.jobCompleted', 'Background task completed'));
      } else if (status === 'failed') {
        toast.error(t('chat.notifications.jobFailed', 'Background task needs attention'));
      } else if (status === 'cancelled') {
        toast.info(t('chat.notifications.jobCancelled', 'Background task cancelled'));
      }
    }
    terminalStatusRef.current = current;
  }, [activeChatId, backgroundJobs, t]);
  const backgroundViewAvailable = backgroundJobs.length > 0;
  // A default/unchanged title is not evidence of an empty transcript (for
  // example, a chat materialized by attachment upload before its first Turn).
  const showConversation = activeChatStartedThisView
    || Boolean(activeHistoryWindow?.items.length)
    || (activeChatIsPersisted && !activeChatLooksEmpty);
  const historyReady =
    (
      activeRunDiscoveryStatus === 'ready' &&
      (
        !activeChatId ||
        (!activeChatIsPersisted && !chatSessions.isPending) ||
        activeHistory.data !== undefined ||
        activeHistory.isError
      )
    );
  const activeRunDiscoveryDisabledReason =
    activeRunDiscoveryStatus === 'error'
        ? t('composer.active_run_discovery_failed', 'Could not check active agent state. Refresh or retry in a moment.')
        : null;
  useEffect(() => {
    if (!activeChatId) return;
    if (activeChatSession) {
      let active = true;
      queueMicrotask(() => {
        if (!active) return;
        if (!activeChatLooksEmpty) markChatStarted(activeChatId);
      });
      return () => {
        active = false;
      };
    }
  }, [activeChatId, activeChatLooksEmpty, activeChatSession, markChatStarted]);
  const previewResources = useMemo(() => {
    const byId = new Map<string, ChatPreviewItem>();
    const add = (item: ChatPreviewItem | null) => {
      if (!item || byId.has(item.id)) return;
      byId.set(item.id, item);
    };
    if (backgroundViewAvailable && activeChatIsPersisted && activeChatId) {
      add({
        id: `background_jobs:${activeChatId}`,
        title: t('chat.background.title', 'Background tasks'),
        resource: {
          schemaVersion: 1,
          kind: 'background_jobs',
          chatId: activeChatId,
        },
      });
    }
    for (const item of previewItems) add(item);
    for (const message of mergeChunks([...(activeHistoryWindow?.items ?? []), ...livePreviewChunks])) {
      for (const call of message.tool_calls) {
        const workflowId = workflowIdFromToolCall(call);
        if (workflowId) {
          add({
            id: `workflow:${workflowId}`,
            title: t('chat.preview.workflowTitle', 'Workflow: {{id}}', { id: workflowId.slice(0, 8) }),
            resource: { schemaVersion: 1, kind: 'workflow', workflowId },
          });
        }
        add(previewItemFromToolCall(call, selectedProjectId));
      }
    }
    return [...byId.values()];
  }, [activeChatId, selectedProjectId, activeChatIsPersisted, activeHistoryWindow?.items, backgroundViewAvailable, livePreviewChunks, previewItems, t]);
  const previewDiscoveryReady = !activeHistory.isLoading && !workspace.isLoading;
  useEffect(() => {
    if (!previewOpen || !previewDiscoveryReady) return;
    const restored = previewItemToRestore(previewItems, previewResources, activePreviewId);
    if (!restored) {
      if (previewItems.length === 0) closePreviewPane();
      return;
    }
    setPreviewItems((current) => current.some((item) => item.id === restored.id)
      ? current
      : [...current, restored]);
    setActivePreviewId(restored.id);
  }, [
    activePreviewId,
    previewDiscoveryReady,
    previewItems,
    previewOpen,
    previewResources,
    setActivePreviewId,
    setPreviewItems,
    closePreviewPane,
  ]);
  const shortChatId = activeChatId ? activeChatId.slice(0, 8) : '';
  const debugAvailable = !!boot.data?.debug_view_enabled;
  const debugPaneVisible = debugOpen && !!workspaceScopeId && showConversation;
  const activePreviewItem = previewItems.find((item) => item.id === activePreviewId) ?? previewItems[0] ?? null;
  const previewPaneMinWidth =
    activePreviewItem?.resource.kind === 'workflow'
      ? WORKFLOW_PREVIEW_PANE_MIN_WIDTH
      : PREVIEW_PANE_MIN_WIDTH;
  const paneLayoutScope = `${accountNamespace}:main-chat`;
  const paneLayoutKey = `chat-panes:${paneLayoutScope}:${previewOpen ? 'view' : 'no-view'}:${debugPaneVisible ? 'debug' : 'no-debug'}`;
  const defaultPaneLayout = useMemo(
    () => loadChatPaneLayout(paneLayoutScope, previewOpen, debugPaneVisible),
    [debugPaneVisible, paneLayoutScope, previewOpen],
  );
  const persistPaneLayout = useCallback(
    (layout: ResizableLayout) => saveChatPaneLayout(paneLayoutScope, previewOpen, debugPaneVisible, layout),
    [debugPaneVisible, paneLayoutScope, previewOpen],
  );
  useEffect(
    () => () => {
      if (chatIdCopyResetTimer.current !== null) {
        window.clearTimeout(chatIdCopyResetTimer.current);
      }
    },
    [],
  );

  useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (!active) return;
      setChatIdCopied(false);
      if (chatIdCopyResetTimer.current !== null) {
        window.clearTimeout(chatIdCopyResetTimer.current);
        chatIdCopyResetTimer.current = null;
      }
    });
    return () => {
      active = false;
    };
  }, [activeChatId]);

  const copyActiveChatId = useCallback(async () => {
    if (!activeChatId) return;
    await navigator.clipboard?.writeText(activeChatId);
    setChatIdCopied(true);
    if (chatIdCopyResetTimer.current !== null) {
      window.clearTimeout(chatIdCopyResetTimer.current);
    }
    chatIdCopyResetTimer.current = window.setTimeout(() => {
      setChatIdCopied(false);
      chatIdCopyResetTimer.current = null;
    }, 3000);
  }, [activeChatId]);

  useEffect(() => {
    if (!debugAvailable) queueMicrotask(() => setDebugOpen(false));
  }, [debugAvailable, setDebugOpen]);

  return (
    <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden bg-surface-work">
      <header className="surface-topbar flex min-h-[60px] shrink-0 items-center gap-2 px-3 py-2 sm:gap-3 sm:px-5">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <div key={activeChatId || 'new-chat'} className="chat-context-transition min-w-0">
            <h1 className="truncate text-[15px] font-semibold leading-5 text-content-primary sm:text-base">
              {activeProject?.name ?? t('nav.projects', 'Projects')}
            </h1>
            <div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-xs text-content-tertiary">
              <span className="truncate">{activeChatTitle}</span>
              {activeChatId ? (
                <>
                  <span aria-hidden="true">·</span>
                  <button
                    type="button"
                    className="group -mx-1 inline-flex min-h-6 min-w-0 items-center gap-1 rounded-sm px-1 font-mono transition-colors duration-feedback hover:text-content-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    title={`${t('chat.copyChatId', 'Copy chat ID')}: ${activeChatId}`}
                    aria-label={t('chat.copyChatId', 'Copy chat ID')}
                    onClick={() => void copyActiveChatId()}
                  >
                    <span className="truncate">{shortChatId}</span>
                    {chatIdCopied ? (
                      <CheckCircle2 className="h-3 w-3 shrink-0 text-state-success" />
                    ) : (
                      <Copy className="h-3 w-3 shrink-0 opacity-0 transition-opacity group-hover:opacity-70 group-focus-visible:opacity-70" />
                    )}
                  </button>
                </>
              ) : null}
              {showChatExecutionStatus ? (
                <>
                  <span aria-hidden="true">·</span>
                  <span className="inline-flex shrink-0 items-center gap-1.5">
                    <StatusDot status={chatStatus.tone} pulse={chatStatus.pulse} />
                    {chatStatus.label}
                  </span>
                </>
              ) : null}
            </div>
          </div>
        </div>
        <Button
          variant="outline"
          size="icon"
          onClick={() => {
            if (!carrierScopeId || !selectedProjectId) return;
            activeRunDiscoveryGenerationRef.current += 1;
            openProjectChatDraft({ account, projectId: selectedProjectId, scopeId: carrierScopeId, startedChatIds: chatSessionItems.map((chat) => chat.chat_id) });
            setActiveRunDiscoveryStatus('ready');
          }}
          disabled={!carrierScopeId || !selectedProjectId}
          data-action="chat-new"
          aria-label={t('new_chat', 'New Chat')}
          title={t('new_chat', 'New Chat')}
          className="shrink-0 sm:h-8 sm:w-auto sm:px-3"
        >
          <Plus className="h-4 w-4" />
          <span className="hidden sm:inline">{t('new_chat', 'New Chat')}</span>
        </Button>
        <ChatShareDialog key={activeChatId} chatId={activeChatId ?? ''} disabled={!activeChatIsPersisted} />
        <ChatToolbarTooltip label={t('chat.toolbar.preview', 'Preview page')}>
          <Button
            ref={previewToggleButtonRef}
            variant={previewOpen ? 'secondary' : 'ghost'}
            size="sm"
            onClick={() => {
              if (previewOpen) {
                closePreviewPane();
                return;
              }
              const target =
                previewItems.find((item) => item.id === activePreviewId) ??
                previewResources.find((item) => item.id === activePreviewId) ??
                previewItems[0] ??
                previewResources[0] ??
                null;
              if (target) openPreviewItem(target);
            }}
            disabled={previewItems.length === 0 && previewResources.length === 0}
            aria-label={t('chat.toolbar.preview', 'Preview page')}
            aria-pressed={previewOpen}
            data-action="chat-preview-toggle"
            className="h-8 shrink-0 gap-1.5 px-2.5 text-muted-foreground hover:text-foreground"
          >
            <Eye className="h-4 w-4" />
            <span className="hidden sm:inline">{t('chat.toolbar.previewShort', 'Preview')}</span>
          </Button>
        </ChatToolbarTooltip>
        {backgroundViewAvailable && (
        <ChatToolbarTooltip label={t('chat.toolbar.activity', 'Activity')}>
          <Button
            variant={
              activePreviewItem?.resource.kind === 'background_jobs' && previewOpen
                ? 'secondary'
                : 'ghost'
            }
            size="sm"
            onClick={() => {
              if (!activeChatIsPersisted || !activeChatId) return;
              openPreviewItem({
                id: `background_jobs:${activeChatId}`,
                title: t('chat.background.title', 'Background tasks'),
                resource: {
                  schemaVersion: 1,
                  kind: 'background_jobs',
                  chatId: activeChatId,
                },
              });
            }}
            disabled={!activeChatIsPersisted || !activeChatId}
            aria-label={attentionCount > 0
              ? t('chat.attention.open', 'Open {{count}} items that need attention', { count: attentionCount })
              : t('chat.toolbar.activity', 'Activity')}
            data-action="chat-background-jobs"
            className={cn(
              'h-8 shrink-0 gap-1.5 px-2.5 text-muted-foreground hover:text-foreground',
              attentionCount > 0 && 'text-state-warning hover:text-state-warning',
            )}
          >
            {attentionCount > 0 ? <AlertTriangle className="h-4 w-4" /> : <ListChecks className="h-4 w-4" />}
            <span className="hidden sm:inline">{t('chat.toolbar.activity', 'Activity')}</span>
            {backgroundJobs.length > 0 || attentionCount > 0 ? (
              <span className={cn(
                'min-w-4 rounded-full px-1 text-center text-xs leading-4 text-white',
                attentionCount > 0 ? 'bg-state-warning' : 'bg-focus',
              )}>
                {Math.min(attentionCount || backgroundJobs.length, 99)}
              </span>
            ) : null}
          </Button>
        </ChatToolbarTooltip>
        )}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="sm"
              aria-label={t('chat.toolbar.more', 'More chat actions')}
              className="h-8 shrink-0 gap-1.5 px-2.5 text-muted-foreground hover:text-foreground"
            >
              <MoreHorizontal className="h-4 w-4" />
              <span className="hidden sm:inline">{t('chat.toolbar.moreShort', 'More')}</span>
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-48">
            {debugAvailable ? (
              <DropdownMenuItem
                disabled={!workspaceScopeId}
                onSelect={() => setDebugOpen((v) => !v)}
                data-action="chat-debug-toggle"
              >
                <Bug className="h-4 w-4" />
                {debugOpen ? t('chat.toolbar.closeDebug', 'Close debug') : t('chat.toolbar.debug', 'Debug page')}
              </DropdownMenuItem>
            ) : null}
            <DropdownMenuItem
              disabled={!carrierScopeId}
              onSelect={() => setExplorerOpen((v) => !v)}
              data-action="chat-explorer-toggle"
            >
              {explorerOpen ? <PanelRightClose className="h-4 w-4" /> : <PanelRightOpen className="h-4 w-4" />}
              {explorerOpen ? t('chat.toolbar.closeSandbox', 'Close sandbox') : t('chat.toolbar.sandbox', 'Sandbox files')}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </header>

      {boot.isLoading && !activeHistory.isSuccess ? (
        <main className="flex flex-1">
          <AsyncState kind="loading" title={t('chat.loading', 'Preparing chat workspace...')} />
        </main>
      ) : (boot.isError && !activeHistory.isSuccess) || !carrierScopeId ? (
        <main className="flex flex-1">
          <AsyncState
            kind="error"
            title={t('chat.error', 'Chat is unavailable.')}
            actionLabel={t('common.retry', 'Retry')}
            onAction={() => void boot.refetch()}
          />
        </main>
      ) : (
        <>
          <SSEStatusBanner wfId={carrierScopeId} activeChatId={activeChatId} />
          <div className="flex min-h-0 flex-1">
            <ResizableGroup
              key={paneLayoutKey}
              id={paneLayoutKey}
              orientation="horizontal"
              className="min-w-0 flex-1"
              defaultLayout={defaultPaneLayout}
              onLayoutChanged={persistPaneLayout}
              resizeTargetMinimumSize={{ coarse: 18, fine: 8 }}
            >
              <ResizablePanel id="chat" minSize={CHAT_PANE_MIN_WIDTH} style={{ overflow: 'hidden' }}>
                <main
                  className={cn(
                    'flex h-full min-h-0 w-full flex-1 flex-col',
                    debugPaneVisible || previewOpen || explorerOpen
                      ? 'max-w-none'
                      : 'mx-auto max-w-[1360px]',
                  )}
                >
                  <div className="flex min-h-0 flex-1 flex-col">
                {projects.isFetched && (projects.data?.length ?? 0) === 0 ? (
                  <div className="flex min-h-0 flex-1 items-center justify-center overflow-y-auto px-6 py-10">
                    <form
                      className="w-full max-w-md rounded-xl border border-edge-subtle bg-surface-raised p-6 shadow-sm"
                      onSubmit={(event) => {
                        event.preventDefault();
                        void createFirstProject();
                      }}
                    >
                      <span className="grid size-10 place-items-center rounded-lg bg-primary/10 text-primary">
                        <FolderPlus className="size-5" />
                      </span>
                      <h2 className="mt-4 text-lg font-semibold text-foreground">
                        {t('nav.projects.emptyTitle', 'Create your first project')}
                      </h2>
                      <p className="mt-1 text-sm leading-6 text-muted-foreground">
                        {t(
                          'nav.projects.createDescription',
                          'Chats in the same project share files and a running workspace. You can add more chats at any time.',
                        )}
                      </p>
                      <label className="mt-5 block text-sm font-medium text-foreground" htmlFor="first-project-name">
                        {t('nav.projects.name', 'Project name')}
                      </label>
                      <Input
                        id="first-project-name"
                        className="mt-2"
                        value={firstProjectName}
                        maxLength={120}
                        autoFocus
                        placeholder={t('nav.projects.namePlaceholder', 'e.g. Research assistant')}
                        onChange={(event) => setFirstProjectName(event.target.value)}
                      />
                      <Button
                        type="submit"
                        className="mt-4 w-full"
                        disabled={!firstProjectName.trim() || createProject.isPending}
                      >
                        {createProject.isPending
                          ? t('common.creating', 'Creating…')
                          : t('nav.projects.createAction', 'Create project')}
                      </Button>
                    </form>
                  </div>
                ) : showConversation ? (
                  <ChatMessageList
                    focusMessageId={searchParams.get('resumeChat') === activeChatId ? searchParams.get('focusMessage') : null}
                    wfId={carrierScopeId}
                    vfsScopeId={workspaceScopeId || carrierScopeId}
                    activeChatId={activeChatId}
                    onOpenWorkflowPreview={(workflowId) => openPreviewItem({
                      id: `workflow:${workflowId}`,
                      title: t('chat.preview.workflowTitle', 'Workflow: {{id}}', { id: workflowId.slice(0, 8) }),
                      resource: { schemaVersion: 1, kind: 'workflow', workflowId },
                    })}
                    historyItems={activeHistoryWindow?.items ?? (activeHistory.data?.items as RawChunk[] | undefined)}
                    historyLoading={activeHistory.isLoading}
                    historyFetching={activeHistory.isFetching}
                    historyError={activeHistory.isError}
                    hasOlderHistory={hasOlderHistory}
                    olderHistoryLoading={olderHistoryLoading}
                    onLoadOlderHistory={loadOlderHistory}
                    persistedChatIds={chatSessionItems.map((s) => s.chat_id)}
                    onOpenFilePreview={(path) => {
                      const fileRef = fileRefFromAgentPath(path, { projectId: selectedProjectId });
                      if (fileRef) openPreviewItem(filePreviewItem(fileRef, fileNameFromPath(path)));
                    }}
                    onOpenInteractivePreview={(artifact) => {
                      const artifactId = artifact.artifact_id || artifact.title || crypto.randomUUID();
                      openPreviewItem({
                        id: `interactive:${artifactId}`,
                        title: artifact.title || t('tool.interactive.untitled', 'Interactive artifact'),
                        resource: { schemaVersion: 1, kind: 'interactive', artifactId },
                        artifact,
                      });
                    }}
                    onSubmitInteractiveAsNewMessage={submitInteractiveAsNewMessage}
                    onOpenBackgroundJobs={({ jobId, deliveryBatchId }) => {
                      if (!activeChatId) return;
                      openPreviewItem({
                        id: `background_jobs:${activeChatId}`,
                        title: t('chat.background.title', 'Background tasks'),
                        resource: {
                          schemaVersion: 1,
                          kind: 'background_jobs',
                          chatId: activeChatId,
                          jobId,
                          deliveryBatchId,
                        },
                      });
                    }}
                  />
                ) : (
                  <div className="flex min-h-0 flex-1 flex-col items-center justify-start overflow-y-auto px-6 pb-20 pt-6 sm:justify-center sm:pt-0">
                    <div
                      className="mb-6 flex max-w-md flex-col items-center text-center"
                      role="log"
                      aria-live="polite"
                      aria-relevant="additions"
                      aria-label={t('agent.conversation', 'Conversation')}
                    >
                      <Sparkles className="mb-3 h-6 w-6 text-muted-foreground" />
                      <h2 className="text-lg font-semibold">
                        {t('chat.empty.title', 'Explore with Agent')}
                      </h2>
                    </div>
                    <div className="chat-composer-width sm:px-5">
                      <ChatTodoDock
                        items={todoItems}
                        collapsed={todoCollapsed}
                        onCollapsedChange={setTodoCollapsed}
                      />
                      <div>
                        <ChatComposer
                          wfId={carrierScopeId}
                          chatId={activeChatId}
                          projectId={selectedProjectId}
                          chatPersisted={activeChatIsPersisted || activeHistory.isSuccess}
                          agentSurface="chat"
                          framed
                          quietFrame
                          showModelSelector
                          historyReady={historyReady}
                          disabledReason={activeRunDiscoveryDisabledReason}
                          onSendStart={() => {
                            markChatStarted(activeChatId);
                          }}
                          onDraftPresenceChange={setComposerHasDraft}
                        />
                      </div>
                      <EmptyChatExamples
                        visible={!showConversation && !composerHasDraft}
                        onSelect={fillComposerExample}
                      />
                    </div>
                  </div>
                )}
                  </div>
                  {showConversation && (
                    <div className="relative shrink-0 bg-surface-work px-4 pb-3 pt-2 before:pointer-events-none before:absolute before:-top-6 before:inset-x-0 before:h-6 before:bg-gradient-to-t before:from-surface-work before:to-transparent">
                      <div className="chat-composer-width sm:px-5">
                        <ChatTodoDock
                          items={todoItems}
                          collapsed={todoCollapsed}
                          onCollapsedChange={setTodoCollapsed}
                        />
                        <div>
                          <ChatComposer
                            wfId={carrierScopeId}
                            chatId={activeChatId}
                            projectId={selectedProjectId}
                            chatPersisted={activeChatIsPersisted || activeHistory.isSuccess}
                            agentSurface="chat"
                            framed
                            quietFrame
                            showModelSelector
                            historyReady={historyReady}
                            disabledReason={activeRunDiscoveryDisabledReason}
                            onSendStart={() => {
                              markChatStarted(activeChatId);
                            }}
                          />
                        </div>
                      </div>
                    </div>
                  )}
                </main>
              </ResizablePanel>
              {previewOpen && (
                <>
                  <ChatPaneSeparator
                    label={t('chat.preview.resize', 'Resize Chat and View panels')}
                  />
                  <ResizablePanel
                    id="preview"
                    minSize={previewPaneMinWidth}
                    groupResizeBehavior="preserve-pixel-size"
                  >
                    <Suspense
                      fallback={(
                        <div className="flex h-full min-h-0 bg-surface-work">
                          <AsyncState
                            kind="loading"
                            title={t('chat.preview.loading', 'Loading preview...')}
                          />
                        </div>
                      )}
                    >
                      <ChatPreviewPane
                        originChatId={activeChatId}
                        open
                        scopeId={carrierScopeId}
                        items={previewItems}
                        resources={previewResources}
                        activeId={activePreviewId}
                        onToggleOpen={(nextOpen) => {
                          if (nextOpen) setPreviewOpen(true);
                          else closePreviewPane();
                        }}
                        onSelect={selectPreviewItem}
                        onOpenResource={openPreviewItem}
                        onOpenInteractiveFile={(path) => {
                          const fileRef = fileRefFromAgentPath(path, { projectId: selectedProjectId });
                          if (fileRef) openPreviewItem(filePreviewItem(fileRef, fileNameFromPath(path)));
                        }}
                        onCloseItem={closePreviewItem}
                        onSubmitInteractiveAsNewMessage={submitInteractiveAsNewMessage}
                      />
                    </Suspense>
                  </ResizablePanel>
                </>
              )}
              {debugPaneVisible && (
                <>
                  <ChatPaneSeparator
                    label={
                      previewOpen
                        ? t('chat.debug.resizeFromView', 'Resize View and Debug panels')
                        : t('chat.debug.resizeFromChat', 'Resize Chat and Debug panels')
                    }
                  />
                  <ResizablePanel
                    id="debug"
                    minSize={DEBUG_PANE_MIN_WIDTH}
                    groupResizeBehavior="preserve-pixel-size"
                  >
                    <ChatDebugPanel
                      key={`${workspaceScopeId}:${activeChatId}`}
                      chatId={activeChatId ?? ''}
                    />
                  </ResizablePanel>
                </>
              )}
            </ResizableGroup>
            {explorerOpen && (
              <aside
                className="relative flex shrink-0 flex-col border-l border-edge-structural bg-surface-work"
                style={{ width: sandboxPane.width }}
              >
                <PaneResizeHandle
                  side="left"
                  width={sandboxPane.width}
                  minWidth={272}
                  maxWidth={560}
                  onWidthChange={sandboxPane.setWidth}
                  onReset={sandboxPane.resetWidth}
                  label={t('chat.sandbox.resize', 'Resize Sandbox panel')}
                />
                <div className="chat-pane-header flex h-11 shrink-0 items-center justify-between px-3">
                  <div className="flex min-w-0 items-center">
                    <span className="truncate text-section">
                      {t('chat.sandbox.title', 'Sandbox')}
                    </span>
                  </div>
                  <div className="flex items-center">
                    <Button
                      variant="ghost"
                      size="icon"
                      className="toolbar-icon-button"
                      aria-label={t('vfs.refresh', 'Refresh')}
                      onClick={() => qc.invalidateQueries({ queryKey: ['vfs'] })}
                    >
                      <RefreshCw className="h-4 w-4" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="toolbar-icon-button"
                      aria-label={t('vfs.collapse', 'Collapse explorer')}
                      onClick={() => setExplorerOpen(false)}
                    >
                      <X className="h-4 w-4" />
                    </Button>
                  </div>
                </div>
                <div className="app-scrollbar min-h-0 flex-1 overflow-auto">
                  {!selectedProjectId ? (
                    <div className="px-4 py-3 text-meta">
                      {t(
                        'chat.explorer.selectProject',
                        'Select or create a project to browse its persistent files.',
                      )}
                    </div>
                  ) : workspace.isLoading || !workspaceScopeId ? (
                    <div className="px-4 py-3 text-meta">
                      {t('chat.explorer.loadingWorkspace', 'Loading workspace...')}
                    </div>
                  ) : (
                    <>
                      <VfsFilesSection
                        wfId={workspaceScopeId}
                        open={explorerOpen}
                        roots={['data', 'memory', 'logs']}
                        selectionKey={sandboxSelectionKey}
                        onSelectionKeyChange={setSandboxSelectionKey}
                        onOpenFile={(path) => {
                          const fileRef = fileRefFromAgentPath(path, { projectId: selectedProjectId });
                          if (fileRef) openPreviewItem(filePreviewItem(fileRef, fileNameFromPath(path)));
                        }}
                      />
                      {mountScopeId && (
                        <VfsFilesSection
                          wfId={mountScopeId}
                          open={explorerOpen}
                          roots={['mount']}
                          selectionKey={sandboxSelectionKey}
                          onSelectionKeyChange={setSandboxSelectionKey}
                          defaultSelectFirst={false}
                          onOpenFile={(path) => {
                            const fileRef = fileRefFromAgentPath(path, { projectId: selectedProjectId });
                            if (fileRef) openPreviewItem(filePreviewItem(fileRef, fileNameFromPath(path)));
                          }}
                        />
                      )}
                    </>
                  )}
                </div>
              </aside>
            )}
          </div>
        </>
      )}
    </div>
  );
}
