import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
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
  Cpu,
  Eye,
  ListChecks,
  MoreHorizontal,
  PanelRightClose,
  PanelRightOpen,
  Plus,
  Power,
  RefreshCw,
  Sparkles,
  X,
} from 'lucide-react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
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
import type { components } from '@/lib/api/schema';
import { useAuthStore } from '@/stores/auth';
import { useChatStreamStore } from '@/stores/chat-stream';
import { useUIStore } from '@/stores/ui';
import { getApiBase } from '@/lib/base-path';
import {
  CHAT_INITIAL_HISTORY_LIMIT,
  fetchChatHistoryPage,
  useChatHistory,
  useChatSessions,
  useChatState,
  useChatWorkspace,
  useGeneralChatBootstrap,
  useChatSandboxStatuses,
} from '@/lib/api/queries/chats';
import { resumeActiveTurn } from '@/lib/api/sse/resume-turn';
import { readServerActiveTurns } from '@/lib/api/sse/server-active-turn';
import {
  CHAT_RECONCILE_INTERVAL_MS,
  reconcileChatWithServer,
} from '@/lib/api/sse/chat-reconcile';
import { runAgentTurn } from '@/lib/api/sse/run-agent-turn';
import { listVfs, readVfs } from '@/lib/api/vfs';
import { cn } from '@/lib/utils';
import {
  EMPTY_CHAT_VIEW_STATE,
  filePreviewItem,
  readChatViewPreferences,
  type ChatPreviewItem,
  writeChatViewPreferences,
} from '@/lib/chat/preview-state';
import { fileRefFromAgentPath } from '@/lib/preview/protocol';
import { chatAccountNamespace, chatClientStateKey } from '@/lib/chat/state-key';
import {
  formatSandboxTtl,
  sandboxTtlRemaining,
  type SandboxLifecycleStatus,
  type SandboxResourceStatus,
} from '@/lib/sandbox-status';
import { mergeHistoryWindow, type ChatHistoryWindow } from './history-window';

const ChatPreviewPane = lazy(() =>
  import('./ChatPreviewPane').then((module) => ({ default: module.ChatPreviewPane })),
);

interface ChatSandboxStatus {
  scope_id: string;
  mount_scope_id?: string | null;
  status: SandboxLifecycleStatus;
  activity_state?: 'busy' | 'idle' | 'unknown';
  idle_elapsed_s?: number | null;
  idle_for_s?: number | null;
  ttl_s?: number | null;
  ttl_paused?: boolean;
  ttl_remaining_s?: number | null;
  closed_for_s?: number | null;
  resources?: SandboxResourceStatus;
}

type DebugMessageType = 'error' | 'synthetic' | 'compressed' | 'ref' | 'abstract' | 'preview' | 'raw';
type DebugRoleType = 'user' | 'assistant' | 'tool';
type DebugFacet = DebugMessageType | DebugRoleType;
type ChatListItem = components['schemas']['ChatListItem'];
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
  chatId: string | null,
): ChatPreviewItem | null {
  if (call.status === 'done') {
    const result = parseStandardToolResult(call.result);
    const path = diagramPreviewPathFromStandardResult(result);
    const fileRef = path ? fileRefFromAgentPath(path, { chatId }) : null;
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
    const fileRef = path ? fileRefFromAgentPath(path, { chatId }) : null;
    if (fileRef) return filePreviewItem(fileRef, fileNameFromPath(path));
  }
  return {
    id: `interactive:${artifactId}`,
    title,
    resource: { schemaVersion: 1, kind: 'interactive', artifactId },
    artifact,
  };
}

interface DebugSnapshotMessage {
  debug_id: string;
  source_message_id?: string | null;
  anchor_source_message_id?: string | null;
  role: string;
  tool_name?: string;
  tool_call_id?: string;
  synthetic?: boolean;
  synthetic_kind?: string;
  form?: string;
  preview_strategy?: string;
  token_field?: string;
  tokens?: number | null;
  token_slots?: Record<string, number | null>;
  path?: string;
  content_type?: string;
  content: string;
  tool_calls?: Array<Record<string, unknown>>;
  error?: boolean;
  runtime_item_type?: string;
  runtime_metadata?: Record<string, unknown>;
  content_truncated?: boolean;
  projection?: 'model_input' | 'turn_output';
}

interface DebugSnapshot {
  schema_version: number;
  kind: string;
  snapshot_id: string;
  chat_id: string;
  thread_id?: string;
  turn_id?: string;
  runtime_type?: 'langchain' | 'codex';
  snapshot_semantics?: 'model_input' | 'runtime_thread_input';
  model_call_index: number;
  created_at: string;
  token_total?: number | null;
  target?: {
    provider?: string;
    model_id?: string;
    context_window_tokens?: number | null;
  };
  runtime_metadata?: Record<string, unknown>;
  memory_config_snapshot?: Record<string, unknown>;
  context_manifest?: Record<string, unknown>;
  context_decisions?: Array<Record<string, unknown>>;
  context_compaction_plan?: Record<string, unknown>;
  tool_registry?: Array<Record<string, unknown>>;
  mcp_catalog?: Array<Record<string, unknown>>;
  runtime_policy?: Record<string, unknown>;
  messages: DebugSnapshotMessage[];
}

type DebugView = 'activity' | 'context' | 'state' | 'raw';

const DEBUG_TYPE_ORDER: DebugMessageType[] = [
  'error',
  'synthetic',
  'compressed',
  'ref',
  'abstract',
  'preview',
  'raw',
];

const DEBUG_TYPE_CLASS: Record<DebugMessageType, string> = {
  error: 'bg-red-500',
  synthetic: 'bg-zinc-400',
  compressed: 'bg-teal-500',
  ref: 'bg-violet-400',
  abstract: 'bg-amber-500',
  preview: 'bg-blue-500',
  raw: 'bg-slate-400',
};

const DEBUG_ROLE_ORDER: DebugRoleType[] = ['user', 'assistant', 'tool'];

const DEBUG_ROLE_CLASS: Record<DebugRoleType, string> = {
  user: 'bg-sky-500',
  assistant: 'bg-emerald-500',
  tool: 'bg-fuchsia-500',
};

const DEBUG_FACET_ORDER: DebugFacet[] = [
  ...DEBUG_TYPE_ORDER,
  ...DEBUG_ROLE_ORDER,
];

const DEBUG_FACET_CLASS: Record<DebugFacet, string> = {
  ...DEBUG_TYPE_CLASS,
  ...DEBUG_ROLE_CLASS,
};

function debugType(message: DebugSnapshotMessage): DebugMessageType {
  if (message.error) return 'error';
  if (message.synthetic) return 'synthetic';
  const form = message.form || 'raw';
  if (form === 'compressed') return 'compressed';
  if (form === 'ref') return 'ref';
  if (form === 'abstract') return 'abstract';
  if (form === 'preview') return 'preview';
  return 'raw';
}

function debugFacetMatches(message: DebugSnapshotMessage, facet: DebugFacet): boolean {
  if ((DEBUG_ROLE_ORDER as readonly string[]).includes(facet)) {
    return message.role === facet;
  }
  return debugType(message) === facet;
}

function formatTokens(n: number | null | undefined): string {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '-';
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k`;
  return String(n);
}

function debugToolCallName(call: Record<string, unknown>): string {
  const fn = call.function;
  if (typeof call.name === 'string' && call.name) return call.name;
  if (fn && typeof fn === 'object' && typeof (fn as Record<string, unknown>).name === 'string') {
    return (fn as Record<string, unknown>).name as string;
  }
  return '(unknown tool)';
}

function debugToolCallId(call: Record<string, unknown>): string {
  return typeof call.id === 'string' && call.id ? call.id : '';
}

function debugToolCallArgs(call: Record<string, unknown>): string {
  const fn = call.function;
  const raw =
    fn && typeof fn === 'object' && 'arguments' in fn
      ? (fn as Record<string, unknown>).arguments
      : call.args ?? call.arguments;
  if (typeof raw === 'string') {
    try {
      return JSON.stringify(JSON.parse(raw), null, 2);
    } catch {
      return raw;
    }
  }
  try {
    return JSON.stringify(raw ?? {}, null, 2);
  } catch {
    return String(raw ?? '');
  }
}

function DebugMessageCard({
  message,
  active,
  onSelect,
}: {
  message: DebugSnapshotMessage;
  active: boolean;
  onSelect: () => void;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const [showMeta, setShowMeta] = useState(false);
  const type = debugType(message);
  const slots = message.token_slots || {};
  const toolCalls = Array.isArray(message.tool_calls) ? message.tool_calls : [];
  const long =
    (message.content || '').length > 1200 ||
    toolCalls.some((call) => debugToolCallArgs(call).length > 1200);
  const copy = () => {
    const payload = [
      message.content || '',
      ...toolCalls.map((call) => {
        const id = debugToolCallId(call);
        return [
          `tool_call: ${debugToolCallName(call)}${id ? ` (${id})` : ''}`,
          debugToolCallArgs(call),
        ].join('\n');
      }),
    ].filter(Boolean).join('\n\n');
    void navigator.clipboard?.writeText(payload);
  };
  return (
    <article
      id={`debug-${message.debug_id}`}
      onDoubleClick={(event) => {
        if ((event.target as HTMLElement).closest('button')) return;
        onSelect();
      }}
      className={cn(
        'rounded-md border bg-background text-xs transition-[border-color,background-color] duration-feedback',
        active && 'border-foreground ring-2 ring-ring/40',
      )}
      data-debug-message-id={message.debug_id}
      data-selected={active ? 'true' : undefined}
    >
      <div className="flex min-w-0 items-start gap-2 border-b px-3 py-2">
        <span className={cn('mt-1 h-2 w-2 shrink-0 rounded-full', DEBUG_TYPE_CLASS[type])} />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-medium">{message.role}</span>
            {message.tool_name && (
              <span className="text-muted-foreground">· {message.tool_name}</span>
            )}
            {message.runtime_item_type && (
              <span className="rounded border px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
                {message.runtime_item_type}
              </span>
            )}
            <span className="text-muted-foreground">
              {message.form || 'raw'} · {formatTokens(message.tokens)} tok
            </span>
            {message.synthetic && (
              <span className="rounded border px-1.5 py-0.5 text-xs uppercase text-muted-foreground">
                injected
              </span>
            )}
            {message.projection === 'turn_output' && (
              <span className="rounded border border-sky-500/30 bg-sky-500/10 px-1.5 py-0.5 text-xs text-sky-700 dark:text-sky-300">
                {t('chat.debug.turnOutputBadge', 'turn output')}
              </span>
            )}
          </div>
          {(message.path || message.content_type || message.tool_call_id) && (
            <div className="mt-1 truncate text-xs text-muted-foreground">
              {message.path || message.content_type || message.tool_call_id}
            </div>
          )}
          {toolCalls.length > 0 && (
            <div className="mt-1 truncate text-xs text-muted-foreground">
              {t('chat.debug.toolCallCount', '{{count}} tool call', { count: toolCalls.length })}: {' '}
              {toolCalls.map(debugToolCallName).join(', ')}
            </div>
          )}
        </div>
        <button
          type="button"
          className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
          onClick={copy}
          title="Copy"
        >
          <Copy className="h-3.5 w-3.5" />
        </button>
      </div>
      {(message.content || toolCalls.length === 0) && (
        <pre
          className={cn(
            'm-0 overflow-auto whitespace-pre-wrap break-words px-3 py-2 font-mono text-xs leading-5',
            expanded ? 'max-h-[60vh]' : 'max-h-[180px]',
          )}
        >
          {message.content}
        </pre>
      )}
      {toolCalls.length > 0 && (
        <div className="space-y-2 border-t bg-muted/20 px-3 py-2">
          {toolCalls.map((call, index) => {
            const id = debugToolCallId(call);
            return (
              <div key={id || index} className="rounded border bg-background">
                <div className="flex items-center gap-2 border-b px-2 py-1.5">
                  <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs uppercase text-muted-foreground">
                    {t('chat.debug.toolCall', 'Tool call')}
                  </span>
                  <span className="font-mono text-xs font-medium">
                    {debugToolCallName(call)}
                  </span>
                  {id && (
                    <span className="truncate font-mono text-xs text-muted-foreground">
                      {id}
                    </span>
                  )}
                </div>
                <pre
                  className={cn(
                    'm-0 overflow-auto whitespace-pre-wrap break-words px-2 py-2 font-mono text-xs leading-5',
                    expanded ? 'max-h-[60vh]' : 'max-h-[180px]',
                  )}
                >
                  {debugToolCallArgs(call)}
                </pre>
              </div>
            );
          })}
        </div>
      )}
      <div className="flex items-center justify-between border-t px-3 py-1.5">
        <div className="truncate text-xs text-muted-foreground">
          {t('chat.debug.raw', 'Raw')} {formatTokens(slots.raw)} · {t('chat.debug.preview', 'Preview')}{' '}
          {formatTokens(slots.preview)} · {t('chat.debug.abstract', 'Abstract')} {formatTokens(slots.abstract)} ·{' '}
          {t('chat.debug.compressed', 'Compressed')} {formatTokens(slots.compressed)}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {long && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-6 px-2 text-xs"
              onClick={() => setExpanded((v) => !v)}
            >
              {expanded ? 'Collapse' : 'Show more'}
            </Button>
          )}
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-6 px-2 text-xs"
            onClick={() => setShowMeta((v) => !v)}
          >
            Meta
          </Button>
        </div>
      </div>
      {showMeta && (
        <div className="grid grid-cols-[140px_minmax(0,1fr)] gap-x-3 gap-y-1 border-t px-3 py-2 text-xs">
          {Object.entries(message).map(([key, value]) => {
            if (key === 'content') return null;
            return (
              <div className="contents" key={key}>
                <div className="text-muted-foreground">{key}</div>
                <div className="min-w-0 break-words font-mono">
                  {typeof value === 'object' ? JSON.stringify(value) : String(value)}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </article>
  );
}

function DebugField({ label, value }: { label: string; value: unknown }) {
  if (value === undefined || value === null || value === '') return null;
  const display = typeof value === 'object' ? JSON.stringify(value) : String(value);
  return (
    <div className="grid grid-cols-[minmax(88px,0.35fr)_minmax(0,1fr)] gap-3 py-1 text-xs">
      <dt className="text-content-tertiary">{label}</dt>
      <dd className="min-w-0 break-words font-mono text-content-secondary">{display}</dd>
    </div>
  );
}

function DebugRecordCard({ title, record }: { title: string; record: Record<string, unknown> }) {
  return (
    <section className="rounded-lg border border-edge-subtle bg-surface-raised px-3 py-2">
      <h3 className="mb-1 truncate text-xs font-semibold text-content-primary">{title}</h3>
      <dl>
        {Object.entries(record).map(([key, value]) => (
          <DebugField key={key} label={key.replaceAll('_', ' ')} value={value} />
        ))}
      </dl>
    </section>
  );
}

function DebugEmpty({ children }: { children: string }) {
  return <div className="rounded-lg border border-dashed border-edge-subtle px-4 py-8 text-center text-sm text-content-tertiary">{children}</div>;
}

function ChatDebugPanel({
  workspaceScopeId,
  scopeId,
  chatId,
}: {
  workspaceScopeId: string;
  scopeId: string;
  chatId: string;
}) {
  const { t } = useTranslation();
  const [debugView, setDebugView] = useState<DebugView>('context');
  const [focusFacet, setFocusFacet] = useState<DebugFacet | null>(null);
  const [focusIndex, setFocusIndex] = useState(0);
  const list = useQuery({
    queryKey: ['chat-debug-snapshots', workspaceScopeId],
    queryFn: () => listVfs({
      wf_id: workspaceScopeId,
      prefix: '/logs/.debug/',
      include_hidden: 'true',
    }),
    enabled: !!workspaceScopeId,
    refetchInterval: 3000,
  });
  const latestPath = useMemo(() => {
    const entries = list.data?.entries ?? [];
    return entries
      .filter((e) => e.path.endsWith('.json'))
      .map((e) => e.path)
      .sort()
      .at(-1) ?? null;
  }, [list.data?.entries]);
  const snapshotQuery = useQuery({
    queryKey: ['chat-debug-snapshot', workspaceScopeId, latestPath],
    queryFn: async () => {
      const out = await readVfs({ wf_id: workspaceScopeId, path: latestPath as string });
      if (typeof out.content !== 'string') return null;
      return JSON.parse(out.content) as DebugSnapshot;
    },
    enabled: !!workspaceScopeId && !!latestPath,
    refetchInterval: 3000,
  });
  const snapshot = snapshotQuery.data;
  const turnOutputQuery = useQuery({
    queryKey: ['chat-debug-turn-output', scopeId, chatId],
    queryFn: () => fetchChatHistoryPage(scopeId, chatId, {
      limit: 200,
      tail: true,
      debug: true,
    }),
    enabled: !!scopeId && !!chatId,
    refetchInterval: 3000,
  });
  const snapshotMessages = useMemo(
    () => snapshot?.messages ?? [],
    [snapshot?.messages],
  );
  const turnOutputMessages = useMemo<DebugSnapshotMessage[]>(() => {
    if (!snapshot?.turn_id) return [];
    const snapshotSeconds = Date.parse(snapshot.created_at) / 1000;
    return (turnOutputQuery.data?.items ?? [])
      .filter((raw): raw is RawChunk => !!raw && typeof raw === 'object')
      .filter((raw) => {
        const meta = raw.meta && typeof raw.meta === 'object' ? raw.meta : {};
        if (meta.turn_id !== snapshot.turn_id || raw.role === 'user') return false;
        // Earlier AI/tool steps already present in the latest model-input
        // snapshot must not be duplicated. Only project durable messages that
        // were committed at or after this model call began.
        return typeof raw.ts !== 'number'
          || !Number.isFinite(snapshotSeconds)
          || raw.ts >= snapshotSeconds;
      })
      .map((raw, index) => {
        const meta = raw.meta && typeof raw.meta === 'object' ? raw.meta : {};
        const toolCalls = Array.isArray(raw.tool_calls)
          ? raw.tool_calls.filter(
              (call): call is Record<string, unknown> => !!call && typeof call === 'object',
            )
          : [];
        const artifact = raw.artifact && typeof raw.artifact === 'object'
          ? raw.artifact
          : {};
        const artifactMeta = artifact.meta && typeof artifact.meta === 'object'
          ? artifact.meta as Record<string, unknown>
          : {};
        const payload = artifact.payload && typeof artifact.payload === 'object'
          ? artifact.payload as Record<string, unknown>
          : {};
        const safeId = String(raw.id || `${snapshot.turn_id}-${index}`)
          .replace(/[^A-Za-z0-9_.:-]/g, '_');
        const status = String(meta.status || artifact.status || '');
        return {
          debug_id: `turn_output_${safeId}`,
          source_message_id: raw.id ? String(raw.id) : null,
          role: raw.role,
          synthetic: false,
          form: 'raw',
          token_field: 'raw',
          tokens: null,
          token_slots: {
            raw: null,
            preview: null,
            abstract: null,
            ref: null,
            compressed: null,
          },
          content: raw.content || '',
          tool_calls: toolCalls,
          tool_call_id: raw.tool_call_id,
          tool_name: typeof meta.tool_name === 'string' ? meta.tool_name : undefined,
          content_type: typeof artifactMeta.content_type === 'string'
            ? artifactMeta.content_type
            : undefined,
          path: typeof payload.ref === 'string' ? payload.ref : undefined,
          error: ['failed', 'error', 'errored', 'cancelled'].includes(status),
          projection: 'turn_output',
          runtime_item_type: 'persistedTurnOutput',
          runtime_metadata: {
            projection: 'turn_output_after_model_input',
            turn_id: snapshot.turn_id,
            persisted_at: raw.ts,
            status: status || undefined,
          },
        };
      });
  }, [snapshot, turnOutputQuery.data?.items]);
  const messages = useMemo(
    () => [...snapshotMessages, ...turnOutputMessages],
    [snapshotMessages, turnOutputMessages],
  );
  const runtimeType = snapshot?.runtime_type ?? 'langchain';
  const runtimeMetadata = snapshot?.runtime_metadata ?? {};
  const codexHistoryIncomplete =
    runtimeType === 'codex' && runtimeMetadata.history_complete === false;
  const snapshotTruncated = runtimeMetadata.snapshot_truncated === true;
  const counts = useMemo(() => {
    const c = Object.fromEntries(DEBUG_FACET_ORDER.map((k) => [k, 0])) as Record<DebugFacet, number>;
    for (const m of messages) {
      c[debugType(m)] += 1;
      if ((DEBUG_ROLE_ORDER as readonly string[]).includes(m.role)) {
        c[m.role as DebugRoleType] += 1;
      }
    }
    return c;
  }, [messages]);
  const focusedMessages = useMemo(
    () => (focusFacet ? messages.filter((m) => debugFacetMatches(m, focusFacet)) : []),
    [focusFacet, messages],
  );
  const activeDebugId = focusedMessages[focusIndex]?.debug_id ?? null;
  const scrollLinkedSource = (message: DebugSnapshotMessage) => {
    const ids = [message.source_message_id, message.anchor_source_message_id]
      .filter((id): id is string => typeof id === 'string' && id.length > 0);
    for (const id of ids) {
      const selector = `[data-source-message-id="${CSS.escape(id)}"]`;
      const target = document.querySelector(selector);
      if (!target) continue;
      target.scrollIntoView({
        block: 'center',
        behavior: 'smooth',
      });
      return;
    }
  };

  const selectDebugMessage = (message: DebugSnapshotMessage) => {
    const facet: DebugFacet = focusFacet && debugFacetMatches(message, focusFacet)
      ? focusFacet
      : debugType(message);
    const idx = messages
      .filter((candidate) => debugFacetMatches(candidate, facet))
      .findIndex((candidate) => candidate.debug_id === message.debug_id);
    setFocusFacet(facet);
    setFocusIndex(Math.max(0, idx));
    requestAnimationFrame(() => {
      document.getElementById(`debug-${message.debug_id}`)?.scrollIntoView({
        block: 'center',
        behavior: 'smooth',
      });
      scrollLinkedSource(message);
    });
  };

  const jump = (facet: DebugFacet, nextIndex: number) => {
    const targets = messages.filter((m) => debugFacetMatches(m, facet));
    if (!targets.length) return;
    const idx = ((nextIndex % targets.length) + targets.length) % targets.length;
    selectDebugMessage(targets[idx]);
  };

  return (
    <aside className="flex h-full min-h-0 w-full min-w-0 flex-col bg-surface-work">
      <div className="chat-pane-header flex h-11 shrink-0 items-center justify-between px-3">
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-2">
            <div className="truncate text-sm font-semibold">
              {t('chat.debug.title', 'Agent Debug')}
            </div>
            {snapshot && (
              <span className="shrink-0 rounded-full border border-edge-subtle bg-surface-sunken px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                {runtimeType}
              </span>
            )}
          </div>
          <div className="truncate text-xs text-muted-foreground">
            {snapshot
              ? runtimeType === 'codex'
                ? t(
                    'chat.debug.codex_summary',
                    '{{count}} input items + {{outputCount}} turn outputs',
                    {
                      count: snapshotMessages.length,
                      outputCount: turnOutputMessages.length,
                    },
                  )
                : t(
                    'chat.debug.langchain_summary',
                    '{{count}} input messages + {{outputCount}} turn outputs · {{tokens}} input tokens',
                    {
                      count: snapshotMessages.length,
                      outputCount: turnOutputMessages.length,
                      tokens: formatTokens(snapshot.token_total),
                    },
                  )
              : t('chat.debug.no_snapshot', 'No debug snapshot yet')}
          </div>
        </div>
        {latestPath && (
          <div className="max-w-[220px] truncate text-xs text-muted-foreground">
            {latestPath.split('/').pop()}
          </div>
        )}
      </div>
      {(codexHistoryIncomplete || snapshotTruncated) && (
        <div className="shrink-0 border-b border-amber-500/25 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-800 dark:text-amber-200">
          {snapshotTruncated
            ? t(
                'chat.debug.snapshot_truncated',
                'This large Runtime snapshot was truncated for safe browser display.',
              )
            : t(
                'chat.debug.codex_history_incomplete',
                'Codex returned a summarized native thread for part of this history.',
              )}
        </div>
      )}
      <div className="chat-pane-subheader flex shrink-0 items-end gap-5 px-3" role="tablist" aria-label={t('chat.debug.views', 'Debug views')}>
        {(['activity', 'context', 'state', 'raw'] as const).map((view) => (
          <button
            key={view}
            type="button"
            role="tab"
            aria-selected={debugView === view}
            onClick={() => setDebugView(view)}
            className={cn(
              'relative h-10 border-b-2 border-transparent text-xs font-medium capitalize text-content-tertiary transition-colors',
              debugView === view && 'border-focus text-content-primary',
            )}
          >
            {t(`chat.debug.${view}`, view[0].toUpperCase() + view.slice(1))}
          </button>
        ))}
      </div>
      {debugView === 'context' && <div className="chat-pane-subheader flex shrink-0 flex-wrap items-center gap-1 px-3 py-1.5">
        {DEBUG_FACET_ORDER.map((facet) => {
          const active = focusFacet === facet;
          const isRoleFacet = (DEBUG_ROLE_ORDER as readonly string[]).includes(facet);
          return (
            <button
              key={facet}
              type="button"
              disabled={!counts[facet]}
              onClick={() => {
                if (active) {
                  setFocusFacet(null);
                  setFocusIndex(0);
                } else {
                  jump(facet, 0);
                }
              }}
              className={cn(
                'flex h-7 items-center gap-1 rounded-md border px-2 text-xs text-muted-foreground disabled:opacity-40',
                isRoleFacet && 'ml-1',
                active && 'border-foreground text-foreground',
              )}
            >
              <span className={cn('h-2 w-2 rounded-full', DEBUG_FACET_CLASS[facet])} />
              <span>{facet}</span>
              <span>{active ? `${Math.min(focusIndex + 1, counts[facet])} / ${counts[facet]}` : counts[facet]}</span>
            </button>
          );
        })}
        {focusFacet && (
          <div className="ml-auto flex items-center gap-1">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-7 px-2 text-xs"
              onClick={() => jump(focusFacet, focusIndex - 1)}
            >
              ↑
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-7 px-2 text-xs"
              onClick={() => jump(focusFacet, focusIndex + 1)}
            >
              ↓
            </Button>
          </div>
        )}
      </div>}
      {debugView === 'context' ? <div className="flex min-h-0 flex-1" role="tabpanel">
        <div className="relative w-3 shrink-0 border-r border-edge-subtle bg-surface-sunken/40">
          {messages.map((m, i) => {
            const type = debugType(m);
            const top = messages.length > 1 ? (i / (messages.length - 1)) * 96 : 2;
            const dim = focusFacet && !debugFacetMatches(m, focusFacet);
            return (
              <button
                key={m.debug_id}
                type="button"
                title={`${m.role} ${m.tool_name || ''} ${m.form || 'raw'}`}
                onClick={() => selectDebugMessage(m)}
                className={cn(
                  'absolute left-1 h-1.5 w-1.5 rounded-full',
                  DEBUG_TYPE_CLASS[type],
                  dim && 'opacity-25',
                  activeDebugId === m.debug_id && 'ring-2 ring-foreground',
                )}
                style={{ top: `${top}%` }}
              />
            );
          })}
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          {list.isLoading || snapshotQuery.isLoading ? (
            <div className="text-sm text-muted-foreground">
              {t('chat.debug.loading', 'Loading debug snapshot...')}
            </div>
          ) : list.isError || snapshotQuery.isError ? (
            <div className="text-sm text-destructive">
              {t('chat.debug.error', 'Failed to load debug snapshot.')}
            </div>
          ) : !snapshot ? (
            <div className="text-sm text-muted-foreground">
              {t('chat.debug.empty', 'No model input snapshot has been written for this chat yet.')}
            </div>
          ) : (
            <div className="space-y-3">
              {snapshotMessages.map((message) => (
                <DebugMessageCard
                  key={message.debug_id}
                  message={message}
                  active={activeDebugId === message.debug_id}
                  onSelect={() => selectDebugMessage(message)}
                />
              ))}
              {turnOutputMessages.length > 0 && (
                <div className="flex items-center gap-2 py-1" data-role="debug-turn-output-divider">
                  <span className="h-px flex-1 bg-edge-subtle" />
                  <span className="rounded-full border border-sky-500/25 bg-sky-500/10 px-2 py-1 text-xs font-medium text-sky-700 dark:text-sky-300">
                    {t('chat.debug.turnOutputDivider', 'Current Turn output · not yet part of this model input')}
                  </span>
                  <span className="h-px flex-1 bg-edge-subtle" />
                </div>
              )}
              {turnOutputMessages.map((message) => (
                <DebugMessageCard
                  key={message.debug_id}
                  message={message}
                  active={activeDebugId === message.debug_id}
                  onSelect={() => selectDebugMessage(message)}
                />
              ))}
            </div>
          )}
        </div>
      </div> : (
        <div className="min-h-0 flex-1 overflow-y-auto p-3" role="tabpanel">
          {!snapshot ? (
            <DebugEmpty>{t('chat.debug.empty', 'No debug snapshot has been written for this chat yet.')}</DebugEmpty>
          ) : debugView === 'activity' ? (
            <div className="space-y-4">
              <section>
                <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-content-tertiary">{t('chat.debug.toolActivity', 'Tool activity')}</h2>
                <div className="space-y-2">
                  {messages.some((message) => message.role === 'tool') ? messages.filter((message) => message.role === 'tool').map((message) => (
                    <DebugRecordCard key={message.debug_id} title={message.tool_name || 'Tool result'} record={{
                      status: message.error ? 'failed' : 'completed',
                      tool_call_id: message.tool_call_id,
                      content_type: message.content_type,
                      path: message.path,
                      tokens: message.tokens,
                    }} />
                  )) : <DebugEmpty>{t('chat.debug.noToolActivity', 'No tool calls recorded in this snapshot.')}</DebugEmpty>}
                </div>
              </section>
              <section>
                <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-content-tertiary">{t('chat.debug.contextDecisions', 'Context decisions')}</h2>
                <div className="space-y-2">
                  {(snapshot.context_decisions ?? []).length ? snapshot.context_decisions?.map((record, index) => (
                    <DebugRecordCard key={index} title={String(record.section_id || record.action || `Decision ${index + 1}`)} record={record} />
                  )) : <DebugEmpty>{t('chat.debug.noDecisions', 'No context decisions recorded.')}</DebugEmpty>}
                </div>
              </section>
              <section>
                <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-content-tertiary">{t('chat.debug.toolsAndMcp', 'Tools and MCP')}</h2>
                <div className="space-y-2">
                  {[...(snapshot.mcp_catalog ?? []), ...(snapshot.tool_registry ?? [])].length ? [...(snapshot.mcp_catalog ?? []), ...(snapshot.tool_registry ?? [])].map((record, index) => (
                    <DebugRecordCard key={index} title={String(record.name || record.capability || `Tool ${index + 1}`)} record={record} />
                  )) : <DebugEmpty>{t('chat.debug.noTools', 'No tool registry activity recorded.')}</DebugEmpty>}
                </div>
              </section>
            </div>
          ) : debugView === 'state' ? (
            <div className="space-y-4">
              <section>
                <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-content-tertiary">{t('chat.debug.runtimePolicy', 'Runtime policy')}</h2>
                <DebugRecordCard title={runtimeType} record={snapshot.runtime_policy ?? {}} />
              </section>
              <section>
                <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-content-tertiary">{t('chat.debug.contextManifest', 'Context manifest')}</h2>
                <DebugRecordCard title={String(snapshot.context_manifest?.mode || 'manifest')} record={snapshot.context_manifest ?? {}} />
              </section>
              <section>
                <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-content-tertiary">{t('chat.debug.memory', 'Session memory')}</h2>
                <DebugRecordCard title={t('chat.debug.memoryConfig', 'Memory configuration')} record={snapshot.memory_config_snapshot ?? {}} />
              </section>
            </div>
          ) : (
            <pre className="max-h-full overflow-auto whitespace-pre-wrap break-words rounded-lg border border-edge-subtle bg-surface-sunken p-3 font-mono text-xs leading-5 text-content-secondary">
              {JSON.stringify({
                snapshot,
                current_turn_output: turnOutputMessages,
              }, null, 2)}
            </pre>
          )}
        </div>
      )}
    </aside>
  );
}

function authHeaders(): HeadersInit | undefined {
  const token = useAuthStore.getState().token;
  return token ? { Authorization: `Bearer ${token}` } : undefined;
}

async function fetchChatSandboxStatus(chatId: string): Promise<ChatSandboxStatus> {
  const base = getApiBase();
  const params = new URLSearchParams({ chat_id: chatId });
  const res = await fetch(`${base}/api/v1/chats/sandbox?${params.toString()}`, {
    headers: authHeaders(),
  });
  if (res.status === 401) {
    useAuthStore.getState().handle401();
    throw new Error('auth');
  }
  if (!res.ok) {
    throw new Error(`chat sandbox status failed: ${res.status}`);
  }
  return (await res.json()) as ChatSandboxStatus;
}

async function closeChatSandbox(chatId: string): Promise<ChatSandboxStatus> {
  const base = getApiBase();
  const params = new URLSearchParams({ chat_id: chatId });
  const res = await fetch(`${base}/api/v1/chats/sandbox?${params.toString()}`, {
    method: 'DELETE',
    headers: authHeaders(),
  });
  if (res.status === 401) {
    useAuthStore.getState().handle401();
    throw new Error('auth');
  }
  if (!res.ok) {
    throw new Error(`chat sandbox close failed: ${res.status}`);
  }
  return (await res.json()) as ChatSandboxStatus;
}

async function startChatSandbox(chatId: string): Promise<ChatSandboxStatus> {
  const base = getApiBase();
  const params = new URLSearchParams({ chat_id: chatId });
  const res = await fetch(`${base}/api/v1/chats/sandbox?${params.toString()}`, {
    method: 'POST',
    headers: authHeaders(),
  });
  if (res.status === 401) {
    useAuthStore.getState().handle401();
    throw new Error('auth');
  }
  if (!res.ok) {
    throw new Error(`chat sandbox start failed: ${res.status}`);
  }
  return (await res.json()) as ChatSandboxStatus;
}

export function ChatPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const account = useAuthStore((state) => state.user);
  const activeChatId = useUIStore((s) => s.activeChatIds.chat);
  const setActiveChatId = useUIStore((s) => s.setActiveChatId);
  const ensureDraftChatSession = useUIStore((s) => s.ensureDraftChatSession);
  const chatEntryIntent = useUIStore((s) => s.chatEntryIntent);
  const setChatEntryIntent = useUIStore((s) => s.setChatEntryIntent);
  const draftChatSessions = useUIStore((s) => s.draftChatSessions);
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
  const [sandboxConfirmOpen, setSandboxConfirmOpen] = useState(false);
  const [sandboxMaterializedChatId, setSandboxMaterializedChatId] = useState<string | null>(null);
  const [sandboxSelectionKey, setSandboxSelectionKey] = useState<string | null>(null);
  const [historyWindows, setHistoryWindows] = useState<Record<string, ChatHistoryWindow>>({});
  const [chatIdCopied, setChatIdCopied] = useState(false);
  const chatIdCopyResetTimer = useRef<number | null>(null);
  const boot = useGeneralChatBootstrap();
  const accountNamespace = chatAccountNamespace(account);
  const sandboxPane = usePersistedPaneWidth({
    storageKey: `vibecanvas:chat-sandbox-pane-width:v1:${accountNamespace}`,
    defaultWidth: 320,
    minWidth: 272,
    maxWidth: 560,
  });
  const carrierScopeId = boot.data?.carrier_scope_id ?? '';
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
    const drafts: ChatListItem[] = draftChatSessions
      .filter(
        (item) =>
          item.scopeId === carrierScopeId &&
          item.surface === 'chat' &&
          !persistedIds.has(item.chat_id),
      )
      .map((item) => ({
        chat_id: item.chat_id,
        scope_id: carrierScopeId,
        chat_context: t('new_chat', 'New Chat'),
        created_at: item.created_at,
      } as ChatListItem));
    const optimistic: ChatListItem[] = optimisticForScope
      .filter((item) => !persistedIds.has(item.chat_id))
      .filter((item) => !drafts.some((draft) => draft.chat_id === item.chat_id))
      .map((item) => ({
        chat_id: item.chat_id,
        scope_id: carrierScopeId,
        chat_context: item.chat_context,
        created_at: item.created_at,
      } as ChatListItem));
    const ids = new Set([
      ...persisted.map((item) => item.chat_id),
      ...drafts.map((item) => item.chat_id),
      ...optimistic.map((item) => item.chat_id),
    ]);
    const activeDraft: ChatListItem[] =
      activeChatId && !ids.has(activeChatId)
        ? [{
            chat_id: activeChatId,
            scope_id: carrierScopeId,
            chat_context: t('new_chat', 'New Chat'),
            created_at: new Date().toISOString(),
          } as ChatListItem]
        : [];
    return [...drafts, ...activeDraft, ...optimistic, ...persisted];
  }, [
    activeChatId,
    carrierScopeId,
    chatSessions.data?.items,
    draftChatSessions,
    optimisticChatSessions,
    t,
  ]);
  const activeChatSession = useMemo(
    () => chatSessionItems.find((s) => s.chat_id === activeChatId) ?? null,
    [activeChatId, chatSessionItems],
  );
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
  const chatSandboxStatuses = useChatSandboxStatuses(chatSessionItems.map((s) => s.chat_id));
  useEffect(() => {
    for (const item of chatSandboxStatuses.data?.items ?? []) {
      qc.setQueryData<ChatSandboxStatus>(['general-chat-sandbox', item.chat_id], (old) => ({
        ...(old ?? {}),
        scope_id: item.scope_id,
        status: item.status,
        activity_state: item.activity_state,
        idle_elapsed_s: item.idle_elapsed_s ?? null,
        idle_for_s: item.idle_for_s ?? null,
        ttl_s: item.ttl_s ?? null,
        ttl_paused: item.ttl_paused,
        ttl_remaining_s: item.ttl_remaining_s ?? null,
        closed_for_s: item.closed_for_s ?? null,
      }));
    }
  }, [chatSandboxStatuses.data?.items, qc]);
  const activeChatIsPersisted = useMemo(
    () =>
      ((chatSessions.data?.items ?? []) as ChatListItem[]).some(
        (s) => s.chat_id === activeChatId,
      ),
    [activeChatId, chatSessions.data?.items],
  );
  // The durable Chat row can become visible just before its authorization
  // projection is queryable. The batch status endpoint is a safe readiness
  // probe: it returns 200 and omits unauthorized/not-yet-projected chats.
  // Gate resource-specific endpoints on the same projection instead of
  // producing transient 404s for an accepted first Turn.
  const activeChatResourcesReady = Boolean(
    activeChatIsPersisted
    && activeChatId
    && chatSandboxStatuses.data?.items.some((item) => item.chat_id === activeChatId),
  );
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
    setSandboxMaterializedChatId(activeChatId);
    if (!control) {
      useUIStore.getState().addOptimisticChatSession({
        scopeId: carrierScopeId,
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
        content,
        control,
        surface: 'main',
        agentSurface: 'chat',
        approvalMode: 'always_allow',
        onAccepted: () => {
          accepted = true;
          resolve();
        },
      }).then(() => {
        if (!accepted) reject(new Error('Continue Turn was not accepted by the backend'));
      });
    });
  }, [activeChatId, carrierScopeId, markChatStarted]);
  const activeChatStartedThisView = !!activeChatId && startedChatIds.has(activeChatId);
  const activeChatLooksEmpty = !activeChatSession ||
    !activeChatSession.chat_context ||
    activeChatSession.chat_context.trim().toLowerCase() === 'new chat' ||
    activeChatSession.chat_context.trim() === t('new_chat', 'New Chat');
  const activeChatIsMaterialized = Boolean(activeChatId) && (
    activeChatIsPersisted ||
    activeChatStartedThisView ||
    sandboxMaterializedChatId === activeChatId
  );
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
  const activeHistory = useChatHistory(
    carrierScopeId || null,
    activeChatIsPersisted && activeProjectionTurnId !== '' ? activeChatId : null,
    activeChatIsPersisted && activeProjectionTurnId !== '',
    activeProjectionTurnId,
  );
  const secondaryChatResourcesReady = Boolean(
    activeChatResourcesReady
    && (
      !activeChatIsPersisted
      || activeHistory.data !== undefined
      || activeHistory.isError
    )
  );
  const workspace = useChatWorkspace(secondaryChatResourcesReady ? activeChatId : null);
  const sandbox = useQuery({
    queryKey: ['general-chat-sandbox', activeChatId],
    queryFn: () => fetchChatSandboxStatus(activeChatId as string),
    enabled: secondaryChatResourcesReady,
    placeholderData: () =>
      activeChatId
        ? qc.getQueryData<ChatSandboxStatus>(['general-chat-sandbox', activeChatId])
        : undefined,
    refetchInterval: 5000,
  });
  const closeSandbox = useMutation({
    mutationFn: (chatId: string) => closeChatSandbox(chatId),
    onSuccess: (data, chatId) => {
      qc.setQueryData(['general-chat-sandbox', chatId], data);
      void qc.invalidateQueries({ queryKey: ['chat-sandbox-statuses'] });
      if (activeChatId === chatId) setSandboxConfirmOpen(false);
    },
  });
  const startSandbox = useMutation({
    mutationFn: (chatId: string) => startChatSandbox(chatId),
    onSuccess: (data, chatId) => {
      qc.setQueryData(['general-chat-sandbox', chatId], data);
      setSandboxMaterializedChatId(chatId);
      if (carrierScopeId) {
        useUIStore.getState().addOptimisticChatSession({
          scopeId: carrierScopeId,
          chat_id: chatId,
          chat_context: t('new_chat', 'New Chat'),
          surface: 'chat',
        });
        qc.setQueryData(['chats', carrierScopeId, 'chat'], (old: { items?: ChatListItem[] } | undefined) => {
          const items = old?.items ?? [];
          if (items.some((item) => item.chat_id === chatId)) return old ?? { items };
          const item: ChatListItem = {
            chat_id: chatId,
            scope_id: carrierScopeId,
            surface: 'chat',
            chat_context: t('new_chat', 'New Chat'),
            created_at: new Date().toISOString(),
            browser_control_status: 'inactive',
          };
          return { ...(old ?? {}), items: [item, ...items] };
        });
        qc.setQueryData(['chat-workspace', chatId], {
          workspace_scope_id: data.scope_id,
          mount_scope_id: data.mount_scope_id ?? null,
          chat_id: chatId,
          current_workflow_id: null,
        });
        void qc.invalidateQueries({ queryKey: ['chats', carrierScopeId, 'chat'] });
      }
      void qc.invalidateQueries({ queryKey: ['chat-sandbox-statuses'] });
      void qc.invalidateQueries({ queryKey: ['vfs', 'list', data.scope_id] });
    },
  });
  const initialChatSelectionRef = useRef(false);
  const activeRunDiscoveryGenerationRef = useRef(0);
  const [activeRunDiscoveryStatus, setActiveRunDiscoveryStatus] = useState<
    'pending' | 'ready' | 'error'
  >('pending');

  useEffect(() => {
    if (!carrierScopeId) return;
    const discoveryGeneration = ++activeRunDiscoveryGenerationRef.current;
    // Explicit navigation intent is already authoritative client state. It
    // must not wait for the durable list or active-run discovery before the
    // requested shell becomes usable.
    if (chatEntryIntent === 'select' && activeChatId) {
      initialChatSelectionRef.current = true;
      setChatEntryIntent(null);
      queueMicrotask(() => setActiveRunDiscoveryStatus('ready'));
      return;
    }
    if (chatEntryIntent === 'default') {
      initialChatSelectionRef.current = true;
      const draftChatId = ensureDraftChatSession(carrierScopeId, 'chat');
      setActiveChatId('chat', draftChatId);
      // This reset is part of atomically selecting a new draft rather than a
      // derived render-only state update.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setSandboxMaterializedChatId(null);
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
      queueMicrotask(() => setActiveRunDiscoveryStatus('ready'));
      return;
    }
    // A hard refresh resets the in-memory UI store. Wait for the durable Chat
    // list before choosing a destination; otherwise the bootstrap races the
    // sessions query, creates a fresh draft, and hides the conversation the
    // user was just viewing (including any pending Continue card).
    if (chatSessions.isPending) return;
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
        setActiveChatId('chat', latestPersistedChat.chat_id);
        markChatStarted(latestPersistedChat.chat_id);
        setSandboxMaterializedChatId(latestPersistedChat.chat_id);
        setChatEntryIntent(null);
        setActiveRunDiscoveryStatus('ready');
        return;
      }
      const draftChatId = ensureDraftChatSession(carrierScopeId, 'chat');
      setActiveChatId('chat', draftChatId);
      setSandboxMaterializedChatId(null);
      setChatEntryIntent(null);
      setActiveRunDiscoveryStatus('ready');
    })();
    return () => {
      disposed = true;
    };
  }, [
    activeChatId,
    carrierScopeId,
    chatEntryIntent,
    chatSessions.data?.items,
    chatSessions.isPending,
    ensureDraftChatSession,
    markChatStarted,
    setActiveChatId,
    setChatEntryIntent,
  ]);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (active) {
        setSandboxConfirmOpen(false);
        setSandboxSelectionKey(null);
      }
    });
    return () => {
      active = false;
    };
  }, [activeChatId]);

  const workspaceScopeId = activeChatIsMaterialized ? (workspace.data?.workspace_scope_id ?? '') : '';
  const mountScopeId = activeChatIsMaterialized ? (workspace.data?.mount_scope_id ?? '') : '';
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
  const backgroundViewAvailable = (
    activeChatSession?.runtime_type !== 'codex'
    || backgroundJobs.length > 0
  );
  const showConversation = activeChatStartedThisView || (activeChatIsPersisted && !activeChatLooksEmpty);
  const activeChatIsDraft = Boolean(
    activeChatId && draftChatSessions.some(
      (item) => item.scopeId === carrierScopeId
        && item.surface === 'chat'
        && item.chat_id === activeChatId,
    ),
  );
  const historyReady =
    activeChatIsDraft || (
      activeRunDiscoveryStatus === 'ready' &&
      (
        !activeChatId ||
        (!activeChatIsPersisted && !chatSessions.isPending) ||
        activeHistory.data !== undefined ||
        activeHistory.isError
      )
    );
  const activeRunDiscoveryDisabledReason =
    !activeChatIsDraft && activeRunDiscoveryStatus === 'error'
        ? t('composer.active_run_discovery_failed', 'Could not check active agent state. Refresh or retry in a moment.')
        : null;
  useEffect(() => {
    if (!activeChatId) return;
    if (activeChatSession) {
      let active = true;
      queueMicrotask(() => {
        if (!active) return;
        if (!activeChatLooksEmpty) markChatStarted(activeChatId);
        setSandboxMaterializedChatId((current) => current === activeChatId ? current : activeChatId);
      });
      return () => {
        active = false;
      };
    }
  }, [activeChatId, activeChatLooksEmpty, activeChatSession, markChatStarted]);
  const sandboxStatus = sandbox.data?.status ?? 'idle';
  const sandboxAllocated = [
    'running',
    'hibernating',
    'hibernated',
    'restoring',
    'releasing',
    'snapshot_failed',
  ].includes(sandboxStatus);
  const sandboxStarting = startSandbox.isPending && startSandbox.variables === activeChatId;
  const sandboxClosing = closeSandbox.isPending && closeSandbox.variables === activeChatId;
  const sandboxTone: SemanticStatus =
    sandboxStatus === 'running'
      ? 'success'
      : sandboxStatus === 'hibernating' || sandboxStatus === 'restoring'
        ? 'running'
      : sandboxStatus === 'closed'
        ? 'danger'
        : 'neutral';
  const sandboxLabel =
    sandboxStatus === 'running'
      ? t('chat.sandbox.running', 'Sandbox running')
      : sandboxStatus === 'hibernating'
        ? t('chat.sandbox.hibernating', 'Creating snapshot')
      : sandboxStatus === 'restoring'
        ? t('chat.sandbox.restoring', 'Restoring sandbox')
      : sandboxStatus === 'releasing'
        ? t('chat.sandbox.releasing', 'Releasing sandbox')
      : sandboxStatus === 'hibernated'
        ? t('chat.sandbox.hibernated', 'Sandbox hibernated')
      : sandboxStatus === 'snapshot_failed'
        ? t('chat.sandbox.snapshot_failed', 'Snapshot failed')
      : sandboxStatus === 'closed'
        ? t('chat.sandbox.closed', 'Sandbox closed')
        : t('chat.sandbox.idle', 'Sandbox idle');
  const sandboxTtlLabel = sandboxAllocated
    ? formatSandboxTtl(sandboxTtlRemaining(sandbox.data))
    : null;
  const sandboxStatusLabel = sandbox.data?.ttl_paused && sandbox.data?.activity_state === 'busy'
    ? `${sandboxLabel} · ${t('chat.sandbox.ttl_paused', 'TTL paused')}`
    : sandboxTtlLabel
    ? `${sandboxLabel} · ${sandboxTtlLabel}`
    : sandboxLabel;
  const runtimeLabel = activeChatSession?.runtime_type === 'codex'
    ? 'Codex'
    : activeChatSession?.runtime_type === 'langchain'
      ? 'LangChain'
      : t('chat.runtime.pending', 'Runtime pending');
  const currentWorkflowId = workspace.data?.current_workflow_id ?? '';
  const previewResources = useMemo(() => {
    const byId = new Map<string, ChatPreviewItem>();
    const add = (item: ChatPreviewItem | null) => {
      if (!item || byId.has(item.id)) return;
      byId.set(item.id, item);
    };
    if (currentWorkflowId) {
      add({
        id: `workflow:${currentWorkflowId}`,
        title: t('chat.preview.workflowTitle', 'Workflow: {{id}}', { id: currentWorkflowId.slice(0, 8) }),
        resource: { schemaVersion: 1, kind: 'workflow', workflowId: currentWorkflowId },
      });
    }
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
        add(previewItemFromToolCall(call, activeChatId));
      }
    }
    return [...byId.values()];
  }, [activeChatId, activeChatIsPersisted, activeHistoryWindow?.items, backgroundViewAvailable, currentWorkflowId, livePreviewChunks, previewItems, t]);
  const previewDiscoveryReady = !activeHistory.isLoading && !workspace.isLoading;
  useEffect(() => {
    if (!previewOpen || previewItems.length > 0 || !previewDiscoveryReady) return;
    const restored =
      previewResources.find((item) => item.id === activePreviewId) ?? previewResources[0] ?? null;
    if (!restored) {
      closePreviewPane();
      return;
    }
    setPreviewItems([restored]);
    setActivePreviewId(restored.id);
  }, [
    activePreviewId,
    previewDiscoveryReady,
    previewItems.length,
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
    <div className="relative flex h-full min-h-0 flex-col bg-surface-work">
      <header className="surface-topbar flex min-h-[60px] shrink-0 items-center gap-2 px-3 py-2 sm:gap-3 sm:px-5">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <div key={activeChatId || 'new-chat'} className="chat-context-transition min-w-0">
            <h1 className="truncate text-[15px] font-semibold leading-5 text-content-primary sm:text-base">
              {activeChatTitle}
            </h1>
            <div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-xs text-content-tertiary">
              <span>{t('chat.title', 'Chat')}</span>
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
              <span className="hidden sm:contents">
                <span aria-hidden="true">·</span>
                <span className="inline-flex shrink-0 items-center gap-1.5">
                  <StatusDot status={sandboxTone} />
                  {sandboxStatusLabel}
                </span>
              </span>
              <span aria-hidden="true">·</span>
              <span
                className="inline-flex min-w-0 items-center gap-1"
                title={t('chat.runtime.current', 'Current runtime: {{runtime}}', { runtime: runtimeLabel })}
              >
                <Cpu className="h-3 w-3 shrink-0" aria-hidden="true" />
                <span className="truncate">{runtimeLabel}</span>
              </span>
            </div>
          </div>
        </div>
        <Button
          variant="outline"
          size="icon"
          onClick={() => {
            if (!carrierScopeId) return;
            const draftChatId = ensureDraftChatSession(carrierScopeId, 'chat');
            activeRunDiscoveryGenerationRef.current += 1;
            // Mark this as an explicit user selection before changing the id.
            // In-flight startup discovery checks this store value and must not
            // overwrite the newly requested empty Chat.
            setChatEntryIntent('select');
            // A user can click New Chat while the initial active-run discovery
            // request is still pending. Changing activeChatId disposes that
            // request; without completing the gate the composer remains in an
            // invisible history-loading state forever. An explicit new draft
            // does not depend on discovery, so it is ready immediately.
            setActiveRunDiscoveryStatus('ready');
            setActiveChatId('chat', draftChatId);
            setSandboxMaterializedChatId(null);
            setChatEntryIntent(null);
          }}
          disabled={!carrierScopeId}
          data-action="chat-new"
          aria-label={t('new_chat', 'New Chat')}
          title={t('new_chat', 'New Chat')}
          className="shrink-0 sm:h-8 sm:w-auto sm:px-3"
        >
          <Plus className="h-4 w-4" />
          <span className="hidden sm:inline">{t('new_chat', 'New Chat')}</span>
        </Button>
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

      {boot.isLoading ? (
        <main className="flex flex-1">
          <AsyncState kind="loading" title={t('chat.loading', 'Preparing chat workspace...')} />
        </main>
      ) : boot.isError || !carrierScopeId ? (
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
              <ResizablePanel id="chat" minSize={CHAT_PANE_MIN_WIDTH}>
                <main
                  className={cn(
                    'flex h-full min-h-0 w-full flex-1 flex-col',
                    debugPaneVisible || previewOpen || explorerOpen
                      ? 'max-w-none'
                      : 'mx-auto max-w-[1360px]',
                  )}
                >
                  <div className="flex min-h-0 flex-1 flex-col">
                {showConversation ? (
                  <ChatMessageList
                    wfId={carrierScopeId}
                    vfsScopeId={workspaceScopeId || carrierScopeId}
                    activeChatId={activeChatId}
                    workflowViewerId={currentWorkflowId || null}
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
                      const fileRef = fileRefFromAgentPath(path, { chatId: activeChatId });
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
                      <div className="chat-composer-shell">
                        <ChatComposer
                          wfId={carrierScopeId}
                          chatId={activeChatId}
                          agentSurface="chat"
                          quietFrame
                          showModelSelector
                          historyReady={historyReady}
                          chatStateReady={secondaryChatResourcesReady}
                          disabledReason={activeRunDiscoveryDisabledReason}
                          onSendStart={() => {
                            markChatStarted(activeChatId);
                            if (activeChatId) setSandboxMaterializedChatId(activeChatId);
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
                        <div className="chat-composer-shell">
                          <ChatComposer
                            wfId={carrierScopeId}
                            chatId={activeChatId}
                            agentSurface="chat"
                            quietFrame
                            showModelSelector
                            historyReady={historyReady}
                            chatStateReady={secondaryChatResourcesReady}
                            disabledReason={activeRunDiscoveryDisabledReason}
                            onSendStart={() => {
                              markChatStarted(activeChatId);
                              if (activeChatId) setSandboxMaterializedChatId(activeChatId);
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
                          const fileRef = fileRefFromAgentPath(path, { chatId: activeChatId });
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
                      workspaceScopeId={workspaceScopeId}
                      scopeId={carrierScopeId ?? ''}
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
                    <div className="relative">
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => {
                          if (sandboxAllocated) {
                            setSandboxConfirmOpen((v) => !v);
                          } else if (activeChatId && !sandboxStarting) {
                            startSandbox.mutate(activeChatId);
                          }
                        }}
                        disabled={!carrierScopeId || !activeChatId || sandboxClosing || sandboxStarting}
                        aria-label={
                          sandboxAllocated
                            ? t('chat.sandbox.close_hint', 'Release sandbox')
                            : t('chat.sandbox.start_hint', 'Start sandbox')
                        }
                        title={
                          sandboxAllocated
                            ? t('chat.sandbox.close_hint', 'Release sandbox')
                            : t('chat.sandbox.start_hint', 'Start sandbox')
                        }
                        className="toolbar-icon-button"
                        data-action="chat-sandbox-status"
                        aria-expanded={sandboxConfirmOpen}
                      >
                        {sandboxClosing || sandboxStarting ? (
                          <RefreshCw className="h-4 w-4 animate-spin" />
                        ) : (
                          <Power className="h-4 w-4" />
                        )}
                      </Button>
                    </div>
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
                  {!activeChatIsMaterialized ? (
                    <div className="px-4 py-3 text-meta">
                      {t(
                        'chat.explorer.emptyDraft',
                        'Send a message or start the sandbox to create this chat workspace.',
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
                          const fileRef = fileRefFromAgentPath(path, { chatId: activeChatId });
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
                            const fileRef = fileRefFromAgentPath(path, { chatId: activeChatId });
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
          <Dialog
            open={sandboxConfirmOpen && sandboxAllocated}
            onOpenChange={setSandboxConfirmOpen}
          >
            <DialogContent data-role="sandbox-close-confirm">
              <DialogHeader>
                <DialogTitle>{t('chat.sandbox.confirm_close', 'Close sandbox?')}</DialogTitle>
                <DialogDescription>
                  {t(
                    'chat.sandbox.confirm_close_description',
                    'This releases the current chat sandbox. Files already saved in the workspace remain available.',
                  )}
                </DialogDescription>
              </DialogHeader>
              <DialogFooter>
                <Button type="button" variant="outline" onClick={() => setSandboxConfirmOpen(false)}>
                  {t('cancel', 'Cancel')}
                </Button>
                <Button
                  type="button"
                  variant="destructive"
                  disabled={sandboxClosing}
                  onClick={() => activeChatId && closeSandbox.mutate(activeChatId)}
                >
                  {sandboxClosing
                    ? t('chat.sandbox.closing', 'Closing...')
                    : t('chat.sandbox.close', 'Close')}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </>
      )}
    </div>
  );
}
