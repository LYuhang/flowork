/**
 * The Agent chat message body: merges persisted history with the live SSE
 * buffer through `mergeChunks` and renders the result as one ordered list.
 * The single reducer over both sources folds tool-result chunks into their
 * parent assistant message across the live/persisted seam; messages past the
 * history boundary are tagged `streaming` so their tool-call blocks auto-expand.
 * (Extracted from the former ChatSessionList right column.)
 */
import { lazy, memo, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, ArrowUpRight, CheckCircle2, ChevronDown, ChevronRight, CircleAlert, CircleStop, Eye, Globe2, Loader2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Skeleton } from '@/components/ui/skeleton';
import { MessageAvatar, MessageItem } from '@/components/agent-sidebar/MessageItem';
import { workflowIdFromToolCall } from '@/components/agent-sidebar/tool-call-utils';
import type {
  InteractiveArtifact,
  SubmitInteractiveAsNewTurn,
} from '@/components/agent-sidebar/tool-render/InteractiveArtifactBlock';
import { mergeChunks } from '@/components/agent-sidebar/types';
import type { MergedMessage, MergedToolCall, RawChunk } from '@/components/agent-sidebar/types';
import { groupToolActivity } from '@/components/agent-sidebar/chat-render-groups';
import { useChatHistory, useChatSessions } from '@/lib/api/queries/chats';
import {
  useChatStreamStore,
  type RuntimeStartupProgress,
} from '@/stores/chat-stream';
import { useUIStore } from '@/stores/ui';
import { useAuthStore } from '@/stores/auth';
import { cn } from '@/lib/utils';
import { chatClientStateKey } from '@/lib/chat/state-key';
import { ChatMessageSearch } from './ChatMessageSearch';
import { ChatRenderProvider } from './chat-render-context';

const EMPTY_STREAM_BUFFER: RawChunk[] = [];

// Completed tool groups are collapsed by default. Keep the large family of
// specialized result renderers out of the initial transcript bundle and load
// them only when a user expands a group (or a live tool auto-expands it).
const ToolCallBlock = lazy(() => import('@/components/agent-sidebar/ToolCallBlock').then(
  (module) => ({ default: module.ToolCallBlock }),
));

// Forms, approvals and HTML artifacts are optional transcript content. Their
// renderer must not delay ordinary messages while the route starts up.
const InteractiveArtifactBlock = lazy(() => import('@/components/agent-sidebar/tool-render/InteractiveArtifactBlock').then(
  (module) => ({ default: module.InteractiveArtifactBlock }),
));

const RUNTIME_PHASE_REVEAL_DELAY_MS = 400;
const RUNTIME_SLOW_HINT_MS = 8_000;

const RuntimeProgressIndicator = memo(function RuntimeProgressIndicator({
  progress,
}: {
  progress: RuntimeStartupProgress;
}) {
  const { t } = useTranslation();
  const [displayed, setDisplayed] = useState<RuntimeStartupProgress | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setTimeout(() => setDisplayed(progress), RUNTIME_PHASE_REVEAL_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [progress]);

  useEffect(() => {
    if (!displayed) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 100);
    return () => window.clearInterval(timer);
  }, [displayed]);

  if (!displayed) return null;

  const labels: Record<RuntimeStartupProgress['phase'], string> = {
    preparing_environment: t('agent.startup.preparing_environment', 'Preparing environment'),
    queueing: t('agent.startup.queueing', 'Waiting for an available resource'),
    acquiring_sandbox: t('agent.startup.acquiring_sandbox', 'Connecting to sandbox'),
    mounting_workspace: t('agent.startup.mounting_workspace', 'Mounting conversation files'),
    initializing_runtime: t('agent.startup.initializing_runtime', 'Starting Agent Runtime'),
    connecting_model: t('agent.startup.connecting_model', 'Connecting to model'),
    awaiting_first_output: t('agent.startup.awaiting_first_output', 'Waiting for response'),
    running_tool: displayed.label
      ? t('agent.startup.running_named_tool', 'Running {{tool}}', { tool: displayed.label })
      : t('agent.startup.running_tool', 'Running operation'),
    finalizing: t('agent.startup.finalizing', 'Preparing response'),
  };
  const startedAt = Date.parse(displayed.startedAt);
  const elapsedMs = Number.isFinite(startedAt) ? Math.max(0, now - startedAt) : 0;

  return (
    <span className="min-w-0" data-role="agent-runtime-progress">
      <span
        className="block whitespace-normal break-words py-0.5 text-xs font-medium leading-5 text-muted-foreground"
        data-role="agent-startup-phase"
      >
        {labels[displayed.phase]}
        <span className="ml-1 tabular-nums" aria-hidden="true">
          · {(elapsedMs / 1000).toFixed(1)}s
        </span>
      </span>
      {displayed.firstTurn && ['preparing_environment', 'queueing', 'acquiring_sandbox', 'mounting_workspace', 'initializing_runtime'].includes(displayed.phase) && elapsedMs >= RUNTIME_SLOW_HINT_MS ? (
        <span className="mt-1 block max-w-[34rem] text-xs leading-4 text-content-tertiary">
          {t(
            'agent.startup.first_turn_hint',
            'The first use prepares an isolated environment; later turns are usually faster.',
          )}
        </span>
      ) : null}
    </span>
  );
});

const AgentRunningIndicator = memo(function AgentRunningIndicator({
  compact,
  startupProgress,
}: {
  compact: boolean;
  startupProgress: RuntimeStartupProgress | null;
}) {
  const { t } = useTranslation();
  return (
    <div
      className="flex items-start justify-start gap-3"
      data-role="agent-thinking"
      data-message-role="assistant"
      role="status"
      aria-live="polite"
      aria-label={t('agent.thinking', 'Agent is thinking')}
      title={t('agent.thinking', 'Agent is thinking')}
    >
      {!compact && <MessageAvatar label="A" tone="agent" />}
      <div
        className="flex min-h-9 min-w-0 max-w-full items-center gap-2.5 py-2 text-sm text-muted-foreground"
        data-message-content-rail="assistant"
      >
        {startupProgress ? <RuntimeProgressIndicator progress={startupProgress} /> : null}
        <span
          className="inline-flex h-4 shrink-0 items-center gap-1 text-state-running"
          aria-hidden="true"
          data-role="agent-thinking-dots"
        >
          <span className="chat-thinking-dot h-1.5 w-1.5 rounded-full bg-current" />
          <span className="chat-thinking-dot h-1.5 w-1.5 rounded-full bg-current" />
          <span className="chat-thinking-dot h-1.5 w-1.5 rounded-full bg-current" />
        </span>
      </div>
    </div>
  );
});

export interface ChatMessageListProps {
  wfId: string;
  vfsScopeId?: string;
  activeChatId: string | null;
  surface?: 'chat' | 'browser';
  compact?: boolean;
  workflowViewerId?: string | null;
  onOpenWorkflowPreview?: (workflowId: string) => void;
  focusMessageId?: string | null;
  historyItems?: RawChunk[];
  historyLoading?: boolean;
  historyFetching?: boolean;
  historyError?: boolean;
  hasOlderHistory?: boolean;
  olderHistoryLoading?: boolean;
  onLoadOlderHistory?: () => Promise<void> | void;
  persistedChatIds?: string[];
  onOpenFilePreview?: (path: string) => void;
  onOpenInteractivePreview?: (artifact: InteractiveArtifact) => void;
  onSubmitInteractiveAsNewMessage?: SubmitInteractiveAsNewTurn;
  onOpenBackgroundJobs?: (options: {
    jobId?: string;
    deliveryBatchId?: string;
  }) => void;
}

function coalesceRenderableMessages(messages: MergedMessage[]): MergedMessage[] {
  const out: MergedMessage[] = [];
  const indexByMessageId = new Map<string, number>();
  for (const message of messages) {
    const id = message.id || null;
    const existingIndex = id ? indexByMessageId.get(id) : undefined;
    if (existingIndex !== undefined) {
      const previous = out[existingIndex];
      const toolCalls = new Map(previous.tool_calls.map((call) => [call.id, call]));
      for (const call of message.tool_calls) {
        toolCalls.set(call.id, { ...toolCalls.get(call.id), ...call });
      }
      out[existingIndex] = {
        ...previous,
        ...message,
        tool_calls: [...toolCalls.values()],
      };
      continue;
    }

    const next = {
      ...message,
      tool_calls: message.tool_calls.map((call) => ({ ...call })),
    };
    if (id) indexByMessageId.set(id, out.length);
    out.push(next);
  }
  return out;
}

function effectiveToolCall(call: MergedToolCall, groupIsLive: boolean): MergedToolCall {
  if (call.status !== 'running') return call;
  const artifactStatus =
    call.artifact && typeof call.artifact.status === 'string'
      ? call.artifact.status
      : '';
  if (artifactStatus === 'error' || artifactStatus === 'cancelled' || artifactStatus === 'canceled') {
    return { ...call, status: 'error' };
  }
  if (call.result !== undefined || artifactStatus === 'success' || !groupIsLive) {
    return { ...call, status: 'done' };
  }
  return call;
}

function ToolActivityGroup({
  calls,
  streaming,
  wfId,
  vfsScopeId,
  workflowViewerId,
  onOpenWorkflowPreview,
  onOpenFilePreview,
  sourceMessageId,
  showAvatar = true,
  compact = false,
  expansionKey,
}: {
  calls: MergedToolCall[];
  streaming?: boolean;
  wfId?: string;
  vfsScopeId?: string;
  workflowViewerId?: string | null;
  onOpenWorkflowPreview?: (workflowId: string) => void;
  onOpenFilePreview?: (path: string) => void;
  sourceMessageId?: string | null;
  showAvatar?: boolean;
  compact?: boolean;
  expansionKey: string;
}) {
  const { t } = useTranslation();
  const open = useUIStore((s) => s.chatToolExpansion[expansionKey] ?? false);
  const setChatToolExpanded = useUIStore((s) => s.setChatToolExpanded);
  const groupIsLive = !!streaming;
  const effectiveCalls = useMemo(
    () => calls.map((call) => effectiveToolCall(call, groupIsLive)),
    [calls, groupIsLive],
  );
  const current =
    [...effectiveCalls].reverse().find((call) => call.status === 'running') ??
    effectiveCalls[effectiveCalls.length - 1];
  const count = effectiveCalls.length;
  const currentToolName = current?.name ?? t('agent.tool_activity.unknown', 'tool');
  const hasRunningCall = effectiveCalls.some((call) => call.status === 'running');
  const hasFailedCall = effectiveCalls.some((call) => call.status === 'error');
  const isActiveGroup = groupIsLive && hasRunningCall;
  const updateCanvasWorkflowId = [...effectiveCalls]
    .reverse()
    .filter((call) => call.name === 'update_canvas')
    .map(workflowIdFromToolCall)
    .find((id): id is string => !!id);
  const hasUpdateCanvas = effectiveCalls.some((call) => call.name === 'update_canvas');
  const viewerWorkflowTarget = updateCanvasWorkflowId ?? (hasUpdateCanvas ? workflowViewerId : null);

  return (
    <div
      className="flex items-start justify-start gap-3"
      data-message-role="assistant"
      data-tool-activity="true"
      data-source-message-id={sourceMessageId || undefined}
    >
      {!compact && (showAvatar ? <MessageAvatar label="A" tone="agent" /> : <div className="h-9 w-9 shrink-0" />)}
      <div
        className={cn('w-full min-w-0 space-y-1', compact ? 'max-w-[94%]' : 'max-w-[min(100%,820px)]')}
        data-message-content-rail="assistant"
      >
        <div
          className={cn(
            'text-tool transition-colors',
            open
              ? 'rounded-md border border-edge-subtle bg-surface-sunken/40'
              : 'rounded-md bg-transparent',
          )}
        >
          <button
            type="button"
            className={cn(
              'flex w-full items-center gap-2 rounded-md text-left transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/30',
              open
                ? 'px-3 py-2 hover:bg-accent/50'
                : 'px-1.5 py-1.5 hover:bg-muted/35',
            )}
            aria-expanded={open}
            data-action="tool-activity-toggle"
            onClick={() => setChatToolExpanded(expansionKey, !open)}
          >
            {open ? (
              <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            )}
            {isActiveGroup ? (
              <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-state-running motion-reduce:animate-none" />
            ) : hasFailedCall ? (
              <CircleAlert className="h-3.5 w-3.5 shrink-0 text-state-danger" />
            ) : (
              <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-state-success" />
            )}
            <span className="min-w-0 truncate font-medium text-content-secondary" title={currentToolName}>
              {count === 1
                ? t('agent.tool_activity.complete_one', '{{count}} tool used', { count })
                : t('agent.tool_activity.complete_other', '{{count}} tools used', { count })}
              {' '}<span className="font-normal text-muted-foreground">({currentToolName})</span>
            </span>
            {hasFailedCall && !isActiveGroup ? <span className="sr-only">{t('agent.tool_activity.failed', 'Tool activity needs attention')}</span> : null}
          </button>
          {open && (
            <div
              className="max-h-80 space-y-2 overflow-y-auto border-t p-2"
              data-role="tool-activity-details"
            >
              {effectiveCalls.map((call) => (
                <Suspense key={call.id} fallback={<Skeleton className="h-16 w-full" />}>
                  <ToolCallBlock
                    call={call}
                    autoExpand={isActiveGroup && call.status === 'running'}
                    wfId={wfId}
                    vfsScopeId={vfsScopeId}
                    onOpenFilePreview={onOpenFilePreview}
                  />
                </Suspense>
              ))}
            </div>
          )}
        </div>
        {viewerWorkflowTarget && onOpenWorkflowPreview && (
          <button
            type="button"
            className="ml-1 inline-flex min-h-8 items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors duration-feedback hover:bg-accent/70 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            onClick={() => onOpenWorkflowPreview(viewerWorkflowTarget)}
            data-action="view-workflow"
          >
            <Eye className="h-3 w-3" />
            {t('chat.viewWorkflow', 'View workflow')}
          </button>
        )}
      </div>
    </div>
  );
}

const StableToolActivityGroup = memo(ToolActivityGroup, (previous, next) => (
  previous.streaming === next.streaming
  && previous.wfId === next.wfId
  && previous.vfsScopeId === next.vfsScopeId
  && previous.workflowViewerId === next.workflowViewerId
  && previous.onOpenWorkflowPreview === next.onOpenWorkflowPreview
  && previous.onOpenFilePreview === next.onOpenFilePreview
  && previous.sourceMessageId === next.sourceMessageId
  && previous.showAvatar === next.showAvatar
  && previous.compact === next.compact
  && previous.expansionKey === next.expansionKey
  && previous.calls.length === next.calls.length
  && previous.calls.every((call, index) => {
    const other = next.calls[index];
    return other != null
      && call.id === other.id
      && call.status === other.status
      && call.arguments === other.arguments
      && call.result === other.result
      && call.artifact === other.artifact
      && call.invocation === other.invocation;
  })
));

function messageKey(message: MergedMessage, index: number): string {
  // The backend assigns a stable id to every persisted and streamed message.
  // Keeping that id as the React key prevents a cumulative message_replace
  // (one per token) from remounting the Markdown tree on every update.
  if (message.id) return `message:${message.id}`;
  const toolIds = message.tool_calls.map((call) => call.id).join(':');
  const contentPrefix = message.content.slice(0, 48);
  return `${message.role}:${toolIds}:${contentPrefix}:${index}`;
}

function toolGroupKey(item: { calls: MergedToolCall[]; startIndex: number }): string {
  return `tool-group-${item.calls[0]?.id ?? item.startIndex}`;
}

function interactiveArtifactKey(item: { call: MergedToolCall; index: number }): string {
  return `interactive-artifact-${item.call.id}`;
}

export function ChatMessageList({
  wfId,
  vfsScopeId,
  activeChatId,
  surface = 'chat',
  compact = false,
  workflowViewerId,
  onOpenWorkflowPreview,
  focusMessageId: referenceMessageId,
  historyItems: historyItemsProp,
  historyLoading: historyLoadingProp,
  hasOlderHistory = false,
  olderHistoryLoading = false,
  onLoadOlderHistory,
  persistedChatIds,
  onOpenFilePreview,
  onOpenInteractivePreview,
  onSubmitInteractiveAsNewMessage,
  onOpenBackgroundJobs,
}: ChatMessageListProps) {
  const { t } = useTranslation();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const shouldStickToBottomRef = useRef(true);
  const wasAutoStreamingRef = useRef(false);
  const loadingOlderRef = useRef(false);
  const historyFillTargetRef = useRef<number | null>(null);
  const lastAutoFillItemsRef = useRef<RawChunk[] | null>(null);
  const [paginationRevision, setPaginationRevision] = useState(0);
  const paginationRef = useRef<{
    chatKey: string | null;
    settled: boolean;
    anchor: { key: string; offset: number; top: number; height: number };
  } | null>(null);
  const readingAnchorRef = useRef<{ key: string; offset: number } | null>(null);
  const previousStreamAnnouncementRef = useRef({
    chatId: activeChatId,
    streaming: false,
  });
  const [showJumpToBottom, setShowJumpToBottom] = useState(false);
  const [streamAnnouncement, setStreamAnnouncement] = useState('');
  const account = useAuthStore((state) => state.user);
  const chatStateKey = activeChatId
    ? chatClientStateKey({ account, scopeId: wfId, surface, chatId: activeChatId })
    : null;
  const scrollStateKey = chatStateKey ? `${chatStateKey}:scroll` : null;
  const setChatScrollPosition = useUIStore((s) => s.setChatScrollPosition);
  // Only fetch the transcript for a PERSISTED chat (one present in the session
  // list). A freshly-created chat id (New Chat, not yet sent) has no server row
  // yet, so we skip the history fetch entirely — no wasted round-trip and no
  // loading skeletons. The composer is still ready; messages stream live.
  const sessions = useChatSessions(wfId, surface);
  // Subscribe to the individual fields owned by this chat. The store also has
  // legacy top-level mirrors for imperative callers, but using a union-shaped
  // "runtime or whole store" selector here prevents React from reliably
  // observing token-level updates. The keyed runtime is the sole UI source.
  const streamBuffer = useChatStreamStore((s) =>
    activeChatId
      ? s.runtimes[activeChatId]?.buffer ?? EMPTY_STREAM_BUFFER
      : EMPTY_STREAM_BUFFER,
  );
  const runtimeMessages = useChatStreamStore((s) =>
    activeChatId ? s.runtimes[activeChatId]?.messages : undefined,
  );
  const streamState = useChatStreamStore((s) =>
    activeChatId ? s.runtimes[activeChatId]?.state ?? 'idle' : 'idle',
  );
  const waitingForUser = useChatStreamStore((s) =>
    activeChatId ? (s.runtimes[activeChatId]?.waitingForUser ?? false) : false,
  );
  const startupProgress = useChatStreamStore((s) =>
    activeChatId ? s.runtimes[activeChatId]?.startupProgress ?? null : null,
  );
  const runtimeChatId = useChatStreamStore((s) =>
    activeChatId ? s.runtimes[activeChatId]?.chatId : undefined,
  );
  const runtimeProjectionActive = useChatStreamStore((s) =>
    activeChatId ? s.runtimes[activeChatId]?.projectionActive === true : false,
  );
  const runtimeTurnId = useChatStreamStore((s) =>
    activeChatId ? s.runtimes[activeChatId]?.turnId : null,
  );
  const streamMessages = useMemo(
    () =>
      runtimeMessages?.length
        ? runtimeMessages
        : mergeChunks(streamBuffer),
    [runtimeMessages, streamBuffer],
  );
  const bufferBelongsToChat = !!activeChatId && runtimeChatId === activeChatId;
  const isStreaming = bufferBelongsToChat && streamState === 'streaming';
  const isActivelyWorking = isStreaming && !waitingForUser;
  const projectionActive = bufferBelongsToChat && runtimeProjectionActive;

  // Do not ask assistive technology to read every streamed token. Announce the
  // Turn boundaries, then expose the completed transcript through the log
  // landmark below so the user can review it at their own pace.
  useEffect(() => {
    const previous = previousStreamAnnouncementRef.current;
    if (previous.chatId !== activeChatId) {
      previousStreamAnnouncementRef.current = { chatId: activeChatId, streaming: isStreaming };
      setStreamAnnouncement('');
      return;
    }
    if (!previous.streaming && isStreaming) {
      setStreamAnnouncement(t('agent.response_started', 'Agent response started'));
    } else if (previous.streaming && streamState === 'cancelled') {
      setStreamAnnouncement(t('agent.turn_cancelled', 'Turn cancelled'));
    } else if (previous.streaming && !isStreaming) {
      setStreamAnnouncement(t('agent.response_complete', 'Agent response complete'));
    }
    previousStreamAnnouncementRef.current = { chatId: activeChatId, streaming: isStreaming };
  }, [activeChatId, isStreaming, streamState, t]);
  const isPersisted =
    !!activeChatId &&
    (persistedChatIds ??
      ((sessions.data?.items as { chat_id: string }[] | undefined) ?? []).map((s) => s.chat_id)
    ).some((id) => id === activeChatId);
  const activeProjectionTurnId = projectionActive ? runtimeTurnId : null;
  const shouldLoadHistory =
    !!activeChatId &&
    !historyItemsProp &&
    isPersisted;
  // Keep fetching the stable transcript even during a live/resumed turn. A
  // resumed SSE stream replays only the active turn's frames, not the transcript
  // that preceded it, so history remains the base layer and streamMessages are
  // rendered as the active-turn delta on top.
  const history = useChatHistory(
    wfId,
    shouldLoadHistory ? activeChatId : null,
    shouldLoadHistory,
    activeProjectionTurnId || null,
  );

  const historyItems: RawChunk[] = useMemo(
    () => historyItemsProp ?? (history.data?.items as RawChunk[] | undefined) ?? [],
    [history.data?.items, historyItemsProp],
  );
  const historyIsLoading = historyLoadingProp ?? history.isLoading;

  const preparingMessage = useChatStreamStore(state => activeChatId ? state.preparingMessages[activeChatId] : undefined);
  const preparingMessages = useMemo(() => preparingMessage ? mergeChunks([{ ...preparingMessage, id: `preparing:${activeChatId}` }]) : [], [preparingMessage, activeChatId]);
  const historyMessages = useMemo(
    () => coalesceRenderableMessages(mergeChunks(historyItems)),
    [historyItems],
  );
  const liveMessages = useMemo(
    () => coalesceRenderableMessages([...historyMessages, ...streamMessages]),
    [historyMessages, streamMessages],
  );
  // Transcript ownership is explicit, never inferred from matching text. While
  // projectionActive is true, history is the immutable pre-Turn checkpoint and
  // runtime messages exclusively own the tail. The terminal SSE handler first
  // installs durable head history, then turns this flag off atomically.
  const showStream = projectionActive;

  // Buffer messages (the LIVE turn) start after stable history.
  const streamBoundary = historyMessages.length;
  const persistedMessageIds = useMemo(() => new Set(historyMessages.map((message) => message.id)), [historyMessages]);

  const merged = useMemo(() => {
    return [...(showStream ? liveMessages : historyMessages), ...preparingMessages];
  }, [historyMessages, liveMessages, showStream, preparingMessages]);

  const renderItems = useMemo(() => groupToolActivity(merged), [merged]);
  const [groupIdentity, setGroupIdentity] = useState(() => ({
    items: renderItems,
    chatKey: scrollStateKey,
    keys: new Map<string, string>(),
  }));
  let groupKeys = groupIdentity.keys;
  if (groupIdentity.items !== renderItems || groupIdentity.chatKey !== scrollStateKey ||
      (groupKeys.size === 0 && renderItems.some(item => item.kind === 'tool_group'))) {
    const previous = groupIdentity.chatKey === scrollStateKey ? groupIdentity.keys : new Map<string, string>();
    groupKeys = new Map<string, string>();
    const usedKeys = new Set<string>();
    for (const item of renderItems) {
      if (item.kind !== 'tool_group') continue;
      // A page boundary can split a tool group. Reuse an existing call's
      // identity when older calls join it, preserving DOM and expansion state.
      const key = item.calls.map(call => previous.get(call.id))
        .find((value): value is string => !!value && !usedKeys.has(value)) ?? toolGroupKey(item);
      usedKeys.add(key);
      for (const call of item.calls) groupKeys.set(call.id, key);
    }
    setGroupIdentity({ items: renderItems, chatKey: scrollStateKey, keys: groupKeys });
  }
  const captureViewport = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return null;
    const top = el.getBoundingClientRect().top;
    const item = Array.from(el.querySelectorAll<HTMLElement>('[data-chat-render-key]'))
      .find(element => element.getBoundingClientRect().bottom > top);
    return {
      key: item?.dataset.chatRenderKey ?? '',
      offset: item ? item.getBoundingClientRect().top - top : 0,
      top: el.scrollTop,
      height: el.scrollHeight,
    };
  }, []);

  const restoreViewport = useCallback((anchor: { key: string; offset: number; top?: number; height?: number }) => {
    const el = scrollRef.current;
    if (!el) return;
    const item = Array.from(el.querySelectorAll<HTMLElement>('[data-chat-render-key]'))
      .find(element => element.dataset.chatRenderKey === anchor.key);
    if (item) {
      el.scrollTop += item.getBoundingClientRect().top - el.getBoundingClientRect().top - anchor.offset;
    } else if (anchor.top != null && anchor.height != null) {
      el.scrollTop = anchor.top + el.scrollHeight - anchor.height;
    }
  }, []);

  const maybeLoadOlderHistory = useCallback((explicit = false, minimumHeight?: number) => {
    const el = scrollRef.current;
    if (!el || !hasOlderHistory || olderHistoryLoading || loadingOlderRef.current || !onLoadOlderHistory) return;
    if (!explicit && el.scrollTop > 80) return;
    const anchor = captureViewport();
    if (!anchor) return;
    historyFillTargetRef.current ??= minimumHeight ?? el.scrollHeight + 2 * el.clientHeight;
    const pending = { chatKey: scrollStateKey, settled: false, anchor };
    paginationRef.current = pending;
    loadingOlderRef.current = true;
    shouldStickToBottomRef.current = false;
    readingAnchorRef.current = anchor;
    // The request can finish before React commits its state updates. Keep the
    // snapshot until a layout effect observes the committed transcript.
    Promise.resolve().then(onLoadOlderHistory).catch(() => {
      historyFillTargetRef.current = null;
      // Keep the current viewport on failure; the load button remains retryable.
    }).finally(() => {
      if (paginationRef.current !== pending) return;
      pending.settled = true;
      setPaginationRevision(revision => revision + 1);
    });
  }, [captureViewport, hasOlderHistory, olderHistoryLoading, onLoadOlderHistory, scrollStateKey]);

  useLayoutEffect(() => {
    const pending = paginationRef.current;
    if (!pending) return;
    if (pending.chatKey !== scrollStateKey) {
      paginationRef.current = null;
      readingAnchorRef.current = null;
      loadingOlderRef.current = false;
      historyFillTargetRef.current = null;
      return;
    }
    restoreViewport(pending.anchor);
    if (pending.settled && !olderHistoryLoading) {
      paginationRef.current = null;
      loadingOlderRef.current = false;
      readingAnchorRef.current = captureViewport();
    }
  }, [historyItems, renderItems, olderHistoryLoading, paginationRevision, scrollStateKey, restoreViewport, captureViewport]);

  useLayoutEffect(() => {
    // Images and lazy tool cards may acquire their final height after the page
    // commits. Preserve the same reading anchor through those layout changes.
    const content = scrollRef.current?.firstElementChild;
    const observer = content && typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => {
      if (!shouldStickToBottomRef.current && readingAnchorRef.current) {
        restoreViewport(readingAnchorRef.current);
      }
    }) : null;
    if (content) observer?.observe(content);
    return () => {
      observer?.disconnect();
      paginationRef.current = null;
      readingAnchorRef.current = null;
      loadingOlderRef.current = false;
      historyFillTargetRef.current = null;
      lastAutoFillItemsRef.current = null;
    };
  }, [scrollStateKey, restoreViewport]);

  // A history page is measured in durable message rows, while the transcript
  // can collapse many tool rows into one compact activity card.  In that
  // case the first page may be shorter than the viewport, so no user scroll
  // event can ever reach the lazy-load threshold.  Keep paging until the
  // rendered transcript fills two viewports. A user-triggered history load
  // likewise prepends two viewports, even when many durable rows collapse
  // into a single tool card. Each page keeps the existing reading anchor.
  useEffect(() => {
    if (!hasOlderHistory || olderHistoryLoading || !onLoadOlderHistory) return;
    const timer = window.setTimeout(() => {
      const el = scrollRef.current;
      if (!el || loadingOlderRef.current || lastAutoFillItemsRef.current === historyItems) return;
      const target = historyFillTargetRef.current ?? 2 * el.clientHeight;
      if (el.scrollHeight < target || el.clientHeight === 0) {
        lastAutoFillItemsRef.current = historyItems;
        maybeLoadOlderHistory(true, target);
      } else {
        historyFillTargetRef.current = null;
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [
    hasOlderHistory,
    historyItems.length,
    historyItems,
    paginationRevision,
    maybeLoadOlderHistory,
    olderHistoryLoading,
    onLoadOlderHistory,
    renderItems.length,
  ]);

  const updateScrollHint = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    const awayFromBottom = distanceFromBottom > 120;
    shouldStickToBottomRef.current = !awayFromBottom;
    setShowJumpToBottom(awayFromBottom);
    if (scrollStateKey) {
      setChatScrollPosition(scrollStateKey, {
        top: el.scrollTop,
        stickToBottom: !awayFromBottom,
      });
    }
    const anchor = captureViewport();
    readingAnchorRef.current = anchor;
    if (anchor && paginationRef.current) paginationRef.current.anchor = anchor;
    maybeLoadOlderHistory();
  }, [captureViewport, maybeLoadOlderHistory, scrollStateKey, setChatScrollPosition]);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'smooth') => {
    const el = scrollRef.current;
    if (!el) return;
    if (typeof el.scrollTo === 'function') {
      el.scrollTo({ top: el.scrollHeight, behavior });
    } else {
      // jsdom and a few embedded WebViews do not implement Element.scrollTo.
      el.scrollTop = el.scrollHeight;
    }
    shouldStickToBottomRef.current = true;
    setShowJumpToBottom(false);
  }, []);

  useEffect(() => {
    if (!scrollStateKey) return;
    const element = scrollRef.current;
    const frame = requestAnimationFrame(() => {
      const el = element;
      if (!el) return;
      const saved = useUIStore.getState().chatScrollPositions[scrollStateKey];
      if (!saved || saved.stickToBottom) {
        scrollToBottom('auto');
        return;
      }
      el.scrollTop = Math.min(saved.top, Math.max(0, el.scrollHeight - el.clientHeight));
      shouldStickToBottomRef.current = false;
      setShowJumpToBottom(true);
    });
    return () => {
      cancelAnimationFrame(frame);
      const el = element;
      if (!el) return;
      const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
      setChatScrollPosition(scrollStateKey, {
        top: el.scrollTop,
        stickToBottom: distanceFromBottom <= 120,
      });
    };
  }, [scrollStateKey, scrollToBottom, setChatScrollPosition]);

  useEffect(() => {
    const streamJustStarted = isStreaming && !wasAutoStreamingRef.current;
    wasAutoStreamingRef.current = isStreaming;
    if (!isStreaming || loadingOlderRef.current) return;
    if (!streamJustStarted && !shouldStickToBottomRef.current) return;
    requestAnimationFrame(() => scrollToBottom('auto'));
  }, [isStreaming, streamBuffer.length, merged.length, scrollToBottom]);

  const [searchFocus, setSearchFocus] = useState<{ chatId: string; id: string | null; revision: number } | null>(null);
  const searchMessageId = searchFocus?.chatId === activeChatId ? searchFocus.id : null;
  const focusMessageId = searchMessageId ?? referenceMessageId;
  const focusKey = `${activeChatId ?? ''}:${focusMessageId ?? ''}:${searchMessageId ? searchFocus?.revision : ''}`;
  const focusedKey = useRef('');
  const focusLoading = useRef(false);
  const [focusErrorKey, setFocusErrorKey] = useState('');
  useEffect(() => {
    if (!focusMessageId || focusedKey.current === focusKey || focusErrorKey === focusKey || historyIsLoading || olderHistoryLoading) return;
    const container = scrollRef.current;
    if (!container) return;
    const target = Array.from(container.querySelectorAll<HTMLElement>('[data-quotable-message]'))
      .find(element => element.dataset.quotableMessage === focusMessageId);
    if (target) {
      shouldStickToBottomRef.current = false;
      const frame = requestAnimationFrame(() => {
        setShowJumpToBottom(true);
        focusedKey.current = focusKey;
        container.querySelectorAll('[data-reference-highlight]').forEach(element => element.removeAttribute('data-reference-highlight'));
        target.setAttribute('data-reference-highlight', 'true');
        container.scrollTop += target.getBoundingClientRect().top - container.getBoundingClientRect().top - 32;
      });
      return () => cancelAnimationFrame(frame);
    }
    if (hasOlderHistory && onLoadOlderHistory) {
      if (focusLoading.current) return;
      focusLoading.current = true;
      Promise.resolve(onLoadOlderHistory()).catch(() => setFocusErrorKey(focusKey))
        .finally(() => { focusLoading.current = false; });
    } else if (historyItems.length > 0) {
      const frame = requestAnimationFrame(() => setFocusErrorKey(focusKey));
      return () => cancelAnimationFrame(frame);
    }
  }, [focusMessageId, focusKey, focusErrorKey, historyIsLoading, olderHistoryLoading, hasOlderHistory, onLoadOlderHistory, historyItems, renderItems]);

  return (
    <ChatRenderProvider value={{ chatId: activeChatId, surface }}>
    <div className="relative flex min-h-0 flex-1 flex-col">
      {activeChatId && historyItems.length > 0 && <ChatMessageSearch key={activeChatId} chatId={activeChatId} onLocate={id => {
        setSearchFocus(previous => ({ chatId: activeChatId, id, revision: (previous?.revision ?? 0) + 1 }));
        if (!id) scrollRef.current?.querySelectorAll('[data-reference-highlight]').forEach(element => element.removeAttribute('data-reference-highlight'));
      }} />}
      {focusMessageId && focusErrorKey === focusKey && <div role="status" className="px-4 py-2 text-xs text-muted-foreground">{t('composer.context.sourceMissing', 'The referenced message could not be located. Your saved excerpt is unchanged.')}</div>}
      <div
        className="sr-only"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        data-role="agent-stream-announcement"
      >
        {streamAnnouncement}
      </div>
      <div
        ref={scrollRef}
        className="chat-scrollbar min-h-0 flex-1 overflow-y-auto overscroll-y-contain"
        style={{ overflowAnchor: 'none' }}
        data-role="agent-message-list"
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        aria-label={t('agent.conversation', 'Conversation')}
        onScroll={updateScrollHint}
      >
        <div
          className={cn(
            compact ? 'mx-auto flex w-full flex-col' : 'chat-content-width flex flex-col',
            compact ? 'gap-2 px-2.5 py-3' : 'gap-2.5 px-4 py-3.5',
          )}
        >
          {olderHistoryLoading && (
            <div className="flex min-h-9 items-center justify-center py-1 text-xs text-muted-foreground" data-role="agent-history-loading-older">
              {t('agent.loading_older', 'Loading earlier messages...')}
            </div>
          )}
          {hasOlderHistory && !olderHistoryLoading && onLoadOlderHistory ? (
            <div className="flex min-h-9 items-center justify-center py-1">
              <button
                type="button"
                className="rounded-md px-3 py-1.5 text-xs font-medium text-primary transition-colors hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                data-role="agent-history-load-older"
                onClick={() => maybeLoadOlderHistory(true)}
              >
                {t('agent.load_older', 'Load earlier messages')}
              </button>
            </div>
          ) : null}
          {shouldLoadHistory && historyIsLoading && historyItems.length === 0 && !showStream && !preparingMessage ? (
            <div className="space-y-2" data-role="agent-history-loading">
              <Skeleton className="h-10 w-3/4" />
              <Skeleton className="h-10 w-2/3" />
              <Skeleton className="h-10 w-4/5" />
            </div>
          ) : merged.length === 0 && !showStream ? (
            compact && surface === 'browser' ? (
              <section className="px-1 py-8" aria-label={t('embed.welcome.title', 'What would you like to do?')}>
                <div className="mb-5 grid size-12 place-items-center rounded-2xl bg-focus/10 text-focus"><Globe2 className="size-6" aria-hidden /></div>
                <h2 className="text-xl font-semibold tracking-tight">{t('embed.welcome.title', 'What would you like to do?')}</h2>
                <p className="mt-2 text-sm leading-6 text-muted-foreground">{t('embed.welcome.description', 'Research across pages, organize information, or work through a form. Describe your goal to get started.')}</p>
                <div className="mt-6 space-y-2">
                  {[
                    ['research', 'Research a topic', 'Research this topic using reliable sources: '],
                    ['summary', 'Summarize this page', 'Summarize this page and list its key takeaways with source links.'],
                    ['form', 'Help with a form', 'Help me fill out the form on this page with the following information: '],
                  ].map(([key, label, prompt]) => (
                    <button key={key} type="button" disabled={!activeChatId}
                      onClick={() => useChatStreamStore.getState().setDraft(t(`embed.welcome.${key}_prompt`, prompt), activeChatId)}
                      className="group flex w-full items-center justify-between gap-3 rounded-xl border border-edge-subtle bg-surface-raised px-3.5 py-3 text-left text-sm transition-colors hover:border-focus/30 hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus disabled:opacity-50 motion-reduce:transition-none">
                      {t(`embed.welcome.${key}`, label)}<ArrowUpRight className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                    </button>
                  ))}
                </div>
              </section>
            ) : <p className="text-sm text-muted-foreground">{t('agent.empty_ready', 'Send a message to start the conversation.')}</p>
          ) : (
            <>
              {renderItems.map((item) => {
                const key = item.kind === 'tool_group'
                  ? groupKeys.get(item.calls[0]?.id ?? '') ?? toolGroupKey(item)
                  : item.kind === 'interactive_artifact'
                    ? interactiveArtifactKey(item)
                    : messageKey(item.message, item.index);
                return (
                  <div
                    key={key}
                    data-chat-render-key={key}
                    className="min-w-0"
                  >
                    {item.kind === 'tool_group' ? (
                      <StableToolActivityGroup
                        calls={item.calls}
                        streaming={isActivelyWorking && item.endIndex >= streamBoundary}
                        wfId={wfId}
                        vfsScopeId={vfsScopeId}
                        workflowViewerId={workflowViewerId}
                        onOpenWorkflowPreview={onOpenWorkflowPreview}
                        onOpenFilePreview={onOpenFilePreview}
                        sourceMessageId={merged[item.startIndex]?.id}
                        showAvatar={item.showAvatar}
                        compact={compact}
                        expansionKey={`${chatStateKey ?? chatClientStateKey({ account, scopeId: wfId, surface, chatId: 'draft' })}:tool:${key}`}
                      />
                    ) : item.kind === 'interactive_artifact' ? (
                      <Suspense fallback={
                        <div role="status" aria-label={t('loading', 'Loading…')}>
                          <Skeleton className="h-24 w-full rounded-xl" />
                        </div>
                      }>
                        <InteractiveArtifactBlock
                          call={item.call}
                          showAvatar={item.showAvatar}
                          compact={compact}
                          onOpenFilePreview={onOpenFilePreview}
                          onOpenInteractivePreview={onOpenInteractivePreview}
                          onSubmitAsNewMessage={onSubmitInteractiveAsNewMessage}
                        />
                      </Suspense>
                    ) : (
                      <MessageItem
                        message={item.message}
                        actionsEnabled={!!item.message.id && persistedMessageIds.has(item.message.id)}
                        showAvatar={item.showAvatar}
                        compact={compact}
                        onOpenBackgroundJobs={onOpenBackgroundJobs}
                        onOpenFilePreview={onOpenFilePreview}
                        streaming={
                          isActivelyWorking &&
                          item.index >= streamBoundary &&
                          item.index === merged.length - 1
                        }
                      />
                    )}
                  </div>
                );
              })}
              {preparingMessage ? (
                <div role="status" data-role="agent-message-preparing" className="flex items-center gap-2 px-1 py-2 text-xs text-muted-foreground">
                  <Loader2 className="size-3 animate-spin" aria-hidden />
                  {t('agent.preparing_message', 'Preparing message…')}
                </div>
              ) : null}
              {isActivelyWorking ? (
                <AgentRunningIndicator
                  compact={compact}
                  startupProgress={startupProgress}
                />
              ) : null}
              {streamState === 'cancelled' && (
                <div
                  className="flex items-start gap-2.5 rounded-lg border border-state-warning/30 bg-state-warning/5 px-3 py-2.5 text-sm"
                  data-role="agent-turn-cancelled"
                  role="status"
                >
                  <CircleStop className="mt-0.5 h-4 w-4 shrink-0 text-state-warning" aria-hidden="true" />
                  <div className="min-w-0">
                    <p className="font-medium text-foreground">
                      {t('agent.turn_cancelled', 'Turn cancelled')}
                    </p>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      {t(
                        'agent.turn_cancelled_hint',
                        'The active operation was stopped. You can write a new message; changes already made are not undone.',
                      )}
                    </p>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
      {showJumpToBottom && (
        <button
          type="button"
          className="absolute bottom-3 left-1/2 z-10 flex h-9 w-9 -translate-x-1/2 items-center justify-center rounded-full border bg-background/80 text-muted-foreground shadow-md backdrop-blur transition hover:bg-background hover:text-foreground"
          aria-label={t('agent.scroll_to_bottom', 'Scroll to bottom')}
          title={t('agent.scroll_to_bottom', 'Scroll to bottom')}
          onClick={() => scrollToBottom()}
          data-action="agent-scroll-bottom"
        >
          <ArrowDown className="h-4 w-4" />
        </button>
      )}
    </div>
    </ChatRenderProvider>
  );
}
