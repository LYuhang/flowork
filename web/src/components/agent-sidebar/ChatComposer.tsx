import { GoalStatus } from './GoalStatus';
import { extensionOrigin } from '@/lib/extension';
import { useChatState } from '@/lib/api/queries/chats';
import { SkillUsePicker } from './SkillUsePicker';
import type { SkillUseSelection } from '@/lib/api/sse/agent-stream';
import { CONTEXT_DRAFT_RESET_EVENT } from '@/lib/chat/context-draft';
import { discardReloadedDraft, useContextDraft } from '@/lib/chat/use-context-draft';
import { ContextAttachmentCard } from './ContextAttachmentCard';
/**
 * Bottom-half composer in the agent sidebar.
 *
 * Sends a user turn to `POST /api/v1/chat-scopes/{scope_id}/chats/{chat_id}/messages`
 * via the SSE-streaming wrapper (`streamAgentTurn`), then lets the route-signal
 * dispatcher push every frame into `useChatStreamStore` and invalidate the
 * relevant TanStack Query caches on `done`.
 *
 * Lifecycle UX (T11):
 *   - `idle` / `complete`               → Send button (disabled until input)
 *   - `streaming`                       → Stop button (asks backend to cancel)
 *   - `cancelled` / `failed`            → Retry button (resends last input)
 *
 * Stop is a backend semantic operation, not a local transport abort. The button
 * calls the turn cancel endpoint and keeps the SSE connection open so the
 * backend can close any partial assistant/tool messages and persist the
 * checkpoint state consistently.
 *
 * Retry source-of-truth: we stash `{content, attachments}` in
 * `useChatStreamStore.lastInput` at send-time. Retry reads from there and
 * fires `handleSend` again with that content; we explicitly do *not*
 * snapshot `lastInput` to component state because a sidebar re-mount
 * (collapsed → expanded) should preserve the option to retry.
 *
 * Keys: ⌘/Ctrl+Enter sends; Shift+Enter and plain Enter insert a newline. An
 * Enter while an IME composition is active never sends. When the input starts with `/`,
 * a VSCode-style command menu opens: ↑/↓ select, Tab/Enter complete, Esc closes,
 * and continued typing filters by prefix.
 *
 * The store is read with selector hooks so we re-render only when the
 * slices we actually care about change — re-rendering on every `buffer`
 * push would be unnecessary churn.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type ClipboardEvent, type DragEvent, type KeyboardEvent, type SetStateAction } from 'react';
import { flushSync } from 'react-dom';
import { acceptProjectChatDraft } from '@/lib/chat/project-draft';
import { fetchProjectMcpSelection } from '@/lib/api/queries/chats';
import { errorMessage } from '@/lib/api/mutations/error-message';
import { Blocks, BrainCircuit, FileText, Image, Loader2, Paperclip, RotateCcw, Send, SlidersHorizontal, Square, Video } from 'lucide-react';
import { Link, useLocation } from 'react-router';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { runAgentTurn } from '@/lib/api/sse/run-agent-turn';
import type { HitlContinueControl } from '@/lib/api/sse/agent-stream';
import { useAgentRuntimeCapabilities } from '@/lib/api/queries/agent-runtime';
import type { AgentRuntimeCapabilities } from '@/lib/api/agent-runtime';
import { useAgentSettingsStore, type AgentSettings, type ApprovalMode, type ReasoningEffort } from '@/stores/agent-settings';
import {
  getChatAgentSettings,
  useChatAgentSettingsStore,
} from '@/stores/chat-agent-settings';
import { useChatStreamStore } from '@/stores/chat-stream';
import { useAuthStore } from '@/stores/auth';
import { useUIStore } from '@/stores/ui';
import { cancelActiveTurn } from '@/lib/api/cancel-turn';
import { chatClientStateKey } from '@/lib/chat/state-key';
import type { components } from '@/lib/api/schema';
import { parseAgentCommand } from './parseAgentCommand';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuCheckboxItem,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { useMcpServers } from '@/lib/api/queries/mcp-servers';
import {
  useChatBootstrap,
  useProjectMcpSelection,
  useSetProjectMcpSelection,
} from '@/lib/api/queries/chats';
import {
  uploadChatAttachment,
  type ChatFileAttachmentType,
} from '@/lib/api/queries/chats';
import {
  attachmentEmoji,
  contextAttachmentKey,
  findAttachmentMention,
  inferredAttachmentType,
  insertAttachmentMention,
  isFileAttachment,
} from './chat-attachments';
import {
  slashCommandsFromCatalog,
  type SlashCommand,
} from './slash-commands';
import { RuntimeModelPicker } from './RuntimeModelPicker';

/**
 * URL pattern for the pinned-version route (T14). When the current
 * pathname matches this, the sidebar is being viewed alongside a
 * read-only canvas — sending a turn would mutate the latest workflow
 * out from under the user, so we disable Send and surface a hint.
 *
 * Reading the location directly (rather than threading a prop down
 * AppLayout → AgentChatSidebar → ChatComposer) keeps the composer
 * self-sufficient and avoids a four-level prop drill for one flag.
 */
const PINNED_VERSION_PATHNAME_RE =
  /^\/workflow\/[^/]+\/version\/v\d+\.sv\d+$/;

type Attachment = NonNullable<components['schemas']['MessagePostBody']['attachments']>[number];
const EMPTY_ATTACHMENTS: Attachment[] = [];

const MAX_ATTACHMENTS_PER_TURN = 32;

interface PendingUpload {
  id: string;
  composerKey: string;
  name: string;
  type: ChatFileAttachmentType;
  file: File;
  chatId: string;
  scopeId: string;
  projectId?: string | null;
  controller: AbortController;
  status: 'staged' | 'queued' | 'uploading' | 'failed';
  error?: string;
}

export interface ChatComposerProps {
  wfId: string;
  chatId: string | null;
  /** Main-app Project that owns a newly materialized Chat. */
  projectId?: string | null;
  /** The server already knows the Chat's immutable Project binding. */
  chatPersisted?: boolean;
  /**
   * Default composer mode for embedded chat. When `'browser'`, a bare non-slash
   * message is sent as `mode=browser` — the embedded side panel is browser-
   * first, so the user doesn't have to prefix every line with `/browser`.
   * Slash-command overrides are still parsed when allowed by this surface.
   */
  defaultMode?: 'chat' | 'browser';
  /** True when rendered inside the extension side panel. Sets the chat `surface`
   *  to "sidepanel" so the backend activates the side-panel-only `/browser`
   *  command (the main app gets a NOTICE telling the user to use the panel). */
  embedded?: boolean;
  /** Product surface for prompt/tool assembly and slash-command filtering. */
  agentSurface?: 'chat' | 'browser';
  /** Called right before a user turn is optimistically sent. */
  onSendStart?: () => void;
  /** Visual treatment for the main Chat page composer. */
  quietFrame?: boolean;
  /** Single-line attachment chips for the small workflow conversation panel. */
  compactAttachments?: boolean;
  framed?: boolean;
  /** Main Chat page places the agent model picker in the composer footer. */
  showModelSelector?: boolean;
  /** Existing chat transcripts must hydrate before accepting a follow-up turn. */
  historyReady?: boolean;
  /** External product gate, e.g. a browser chat leased by another window. */
  disabledReason?: string | null;
  /** Reports whether text, attachments, or an in-flight upload occupies the draft. */
  onDraftPresenceChange?: (hasDraft: boolean) => void;
  /** Materialize a contextual conversation only at Send; return its Project.
   * Files remain local until this succeeds, so save conflicts preserve drafts.
   * Callers keep this callback idempotent and recheck their editing context.
   */
  prepareConversation?: () => Promise<string>;
  onSendAccepted?: () => void;
}

export function ChatComposer({
  wfId,
  chatId,
  projectId,
  chatPersisted = false,
  defaultMode,
  embedded,
  agentSurface = embedded ? 'browser' : 'chat',
  onSendStart,
  quietFrame = false,
  compactAttachments = true,
  framed = false,
  showModelSelector = false,
  historyReady = true,
  disabledReason = null,
  onDraftPresenceChange,
  prepareConversation,
  onSendAccepted,
}: ChatComposerProps) {
  const { t } = useTranslation();
  const account = useAuthStore((state) => state.user);
  const preparedConversation = useRef<{ chatId: string; projectId: string } | null>(null);
  const currentConversation = useRef(chatId);
  useEffect(() => {
    currentConversation.current = chatId;
    return () => { currentConversation.current = null; };
  }, [chatId]);
  const [draftMcpIds, setDraftMcpIds] = useState<string[] | null>(null);
  const commandBootstrap = useChatBootstrap(agentSurface);
  const slashCommands = useMemo<SlashCommand[]>(
    () => slashCommandsFromCatalog(
      commandBootstrap.data?.available_commands ?? [],
    ),
    [commandBootstrap.data?.available_commands],
  );
  const composerStateKey = chatId
    ? chatClientStateKey({ account, scopeId: wfId, surface: agentSurface, chatId })
    : null;
  const composerStorageKey = composerStateKey
    ? `vibecanvas:chat-composer:v1:${composerStateKey}`
    : null;
  const value = useChatStreamStore((state) =>
    composerStateKey ? state.composerInputs[composerStateKey] ?? '' : '',
  );
  const setComposerInput = useChatStreamStore((s) => s.setComposerInput);
  const [skillPickerOpen, setSkillPickerOpen] = useState(false);
  const [goalPickerOpen, setGoalPickerOpen] = useState(false);
  const goalQuery = useChatState(wfId, chatId, !!chatId);
  const [selectedSkill, setSelectedSkill] = useState<(SkillUseSelection & { scope: string | null }) | null>(null);
  const projectMcpQuery = useProjectMcpSelection(projectId);
  const updateProjectMcp = useSetProjectMcpSelection();
  const runtimeCapabilitiesQuery = useAgentRuntimeCapabilities(chatId, {
    enabled: !!chatId && historyReady,
    projectId,
    persisted: chatPersisted,
  });
  const chatAgentSettings = useChatAgentSettingsStore((state) =>
    chatId ? state.entries[chatId] : undefined,
  );
  const initializeChatAgentSettings = useChatAgentSettingsStore((state) => state.initializeDraft);
  const hydrateBoundChatAgentSettings = useChatAgentSettingsStore((state) => state.hydrateBound);
  const setChatAgentSettings = useChatAgentSettingsStore((state) => state.set);
  const updateChatAgentSettings = useCallback((patch: Partial<AgentSettings>) => {
    if (chatId) setChatAgentSettings(chatId, patch);
  }, [chatId, setChatAgentSettings]);
  useEffect(() => {
    if (!chatId || !runtimeCapabilitiesQuery.data) return;
    const capabilities = runtimeCapabilitiesQuery.data;
    const bound = capabilities.bound_agent_settings;
    if (bound) {
      hydrateBoundChatAgentSettings(chatId, {
        modelId: bound.model_id,
        temperature: bound.temperature,
        maxTokens: bound.max_tokens,
        timeout: bound.timeout,
        reasoningEffort: bound.reasoning_effort,
      });
      return;
    }
    initializeChatAgentSettings(chatId);
  }, [
    chatId,
    hydrateBoundChatAgentSettings,
    initializeChatAgentSettings,
    runtimeCapabilitiesQuery.data,
  ]);
  const mcpServersQuery = useMcpServers({ enabled: !!chatId && historyReady });
  const selectedMcpIds = draftMcpIds ?? projectMcpQuery.data?.mcp_server_ids ?? [];
  // A server can be uninstalled while another page still has its id in the
  // durable selection. Keep temporarily disabled/unhealthy installations so
  // the picker can explain them, but never count or resend an id that is no
  // longer present in the user's installed-server catalog.
  const installedMcpIds = new Set(
    (mcpServersQuery.data ?? []).map((server) => server.id),
  );
  const effectiveSelectedMcpIds = mcpServersQuery.isSuccess
    ? selectedMcpIds.filter((id) => installedMcpIds.has(id))
    : selectedMcpIds;
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [compactOptions, setCompactOptions] = useState(false);
  const setSelectedMcpIds = (ids: string[]) => {
    if (prepareConversation && !projectId) {
      setDraftMcpIds(ids);
      return;
    }
    if (!projectId || !projectMcpQuery.data || updateProjectMcp.isPending) return;
    updateProjectMcp.mutate({
      projectId,
      selection: {
        mcp_server_ids: ids,
        mcp_config_revision: projectMcpQuery.data.mcp_config_revision,
      },
    }, {
      onError: () => toast.error(t('composer.mcp.saveFailed', 'Could not save project MCP settings. Refresh the selection and try again.')),
    });
  };
  const setValue = useCallback((next: SetStateAction<string>) => {
    if (!composerStateKey) return;
    const current = useChatStreamStore.getState().composerInputs[composerStateKey] ?? '';
    setComposerInput(composerStateKey, typeof next === 'function' ? next(current) : next);
  }, [composerStateKey, setComposerInput]);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const imageInputRef = useRef<HTMLInputElement>(null);
  const videoInputRef = useRef<HTMLInputElement>(null);
  const dragDepthRef = useRef(0);
  // Slash-command autocomplete: the highlighted candidate, and a one-shot
  // "dismissed via Esc" flag (reset on the next keystroke so typing reopens it).
  const [activeIdx, setActiveIdx] = useState(0);
  const [menuDismissed, setMenuDismissed] = useState(false);
  const [caretPosition, setCaretPosition] = useState(0);
  const [mentionActiveIdx, setMentionActiveIdx] = useState(0);
  const [mentionDismissed, setMentionDismissed] = useState(false);
  const [uploads, setUploads] = useState<PendingUpload[]>([]);
  const uploadQueue = useRef<PendingUpload[]>([]);
  const uploadQueueBusy = useRef(false);
  useEffect(() => {
    const clear = () => {
      for (const item of uploadQueue.current) item.controller.abort();
      uploadQueue.current = [];
    };
    const reset = () => { clear(); setUploads([]); };
    window.addEventListener(CONTEXT_DRAFT_RESET_EVENT, reset);
    return () => { window.removeEventListener(CONTEXT_DRAFT_RESET_EVENT, reset); clear(); };
  }, []);
  const [dragActive, setDragActive] = useState(false);
  // Soft inline notice (e.g. "no extension connected") shown under the
  // composer when a `/browser` send can't reach an extension. Cleared on the
  // next send attempt so it never lingers.
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return undefined;
    const query = window.matchMedia('(max-width: 639px)');
    const update = () => setCompactOptions(query.matches);
    update();
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);
  const scopedRuntime = useChatStreamStore((s) => (chatId ? s.runtimes[chatId] : undefined));
  const fallbackChatId = useChatStreamStore((s) => s.chatId);
  const fallbackTurnId = useChatStreamStore((s) => s.turnId);
  const fallbackState = useChatStreamStore((s) => s.state);
  const fallbackBuffer = useChatStreamStore((s) => s.buffer);
  const fallbackMessages = useChatStreamStore((s) => s.messages);
  const fallbackTodoItems = useChatStreamStore((s) => s.todoItems);
  const fallbackAbortController = useChatStreamStore((s) => s.abortController);
  const fallbackLastInput = useChatStreamStore((s) => s.lastInput);
  const fallbackRuntime = useMemo(
    () =>
      chatId && fallbackChatId === chatId
        ? {
            chatId,
            turnId: fallbackTurnId,
            state: fallbackState,
            buffer: fallbackBuffer,
            messages: fallbackMessages,
            todoItems: fallbackTodoItems,
            abortController: fallbackAbortController,
            lastInput: fallbackLastInput,
          }
        : undefined,
    [
      chatId,
      fallbackAbortController,
      fallbackBuffer,
      fallbackChatId,
      fallbackLastInput,
      fallbackMessages,
      fallbackState,
      fallbackTodoItems,
      fallbackTurnId,
    ],
  );
  const runtime = scopedRuntime ?? fallbackRuntime;
  const streamState = runtime?.state ?? 'idle';
  const lastInput = runtime?.lastInput ?? null;
  const pendingAttachmentsForChat = useChatStreamStore((state) =>
    composerStateKey ? state.pendingAttachments[composerStateKey] : undefined,
  );
  const pendingAttachments = pendingAttachmentsForChat ?? EMPTY_ATTACHMENTS;
  const fileAttachments = useMemo(
    () => pendingAttachments.filter(isFileAttachment),
    [pendingAttachments],
  );
  const activeUploads = useMemo(
    () => uploads.filter((upload) => upload.composerKey === composerStateKey),
    [composerStateKey, uploads],
  );
  useEffect(() => {
    onDraftPresenceChange?.(
      value.trim().length > 0 || pendingAttachments.length > 0 || activeUploads.length > 0,
    );
  }, [activeUploads.length, onDraftPresenceChange, pendingAttachments.length, value]);
  const removeAttachmentAt = useChatStreamStore((s) => s.removeAttachmentAt);
  const draft = useChatStreamStore((s) => s.draft);
  const consumeDraft = useChatStreamStore((s) => s.consumeDraft);
  const location = useLocation();
  const readOnly = PINNED_VERSION_PATHNAME_RE.test(location.pathname);
  const streamBelongsToThisChat = !!chatId && runtime?.chatId === chatId;
  const isStreaming = streamBelongsToThisChat && streamState === 'streaming';
  const hydratedComposerKeyRef = useRef<string | null>(null);
  // Keep the last durable draft until the backend accepts the optimistic
  // submission. This lets the textarea clear immediately without losing text
  // if the request is rejected before it acquires the Chat turn lease.
  const optimisticSubmissionRef = useRef(false);
  const preparationRef = useRef(false);
  const [draftPreparing, setDraftPreparing] = useState(false);
  const [attachmentCreatedChat, setAttachmentCreatedChat] = useState<string | null>(null);

  const processUploadQueue = useCallback(async () => {
    if (uploadQueueBusy.current) return;
    uploadQueueBusy.current = true;
    try {
      while (uploadQueue.current.length) {
        const item = uploadQueue.current[0];
        if (item.status === 'failed' || item.status === 'staged') break;
        item.status = 'uploading';
        setUploads([...uploadQueue.current]);
        try {
          const attachment = await uploadChatAttachment({ scopeId: item.scopeId, chatId: item.chatId,
            projectId: item.projectId, file: item.file, type: item.type, signal: item.controller.signal });
          if (!item.controller.signal.aborted) {
            useChatStreamStore.getState().addAttachment(item.composerKey, attachment);
            setAttachmentCreatedChat(item.chatId);
          }
          uploadQueue.current = uploadQueue.current.filter(candidate => candidate.id !== item.id);
        } catch (error) {
          if (item.controller.signal.aborted) {
            uploadQueue.current = uploadQueue.current.filter(candidate => candidate.id !== item.id);
          } else {
            item.status = 'failed';
            item.error = error instanceof Error ? error.message : String(error);
            toast.error(t('composer.attachment_upload_failed', { name: item.name, reason: item.error,
              defaultValue: `Could not upload ${item.name}.` }));
          }
        }
        setUploads([...uploadQueue.current]);
      }
    } finally { uploadQueueBusy.current = false; }
  }, [t]);
  const cancelUpload = (item: PendingUpload) => {
    item.controller.abort();
    uploadQueue.current = uploadQueue.current.filter(candidate => candidate.id !== item.id);
    setUploads([...uploadQueue.current]);
    void processUploadQueue();
  };
  const retryUpload = (item: PendingUpload) => {
    item.controller = new AbortController(); item.status = 'queued'; item.error = undefined;
    setUploads([...uploadQueue.current]);
    void processUploadQueue();
  };

  const uploadFiles = useCallback(async (
    inputFiles: readonly File[],
    requestedType?: ChatFileAttachmentType,
  ) => {
    if (inputFiles.length === 0) return;
    if (!chatId || !composerStateKey || readOnly || !historyReady) {
      toast.error(t('composer.attachments_not_ready', 'This conversation is not ready for attachments. Please wait or open an editable conversation.'));
      return;
    }
    // Uploads during a running turn belong to the next draft.

    const capacity = Math.max(
      0,
      MAX_ATTACHMENTS_PER_TURN -
        (useChatStreamStore.getState().pendingAttachments[composerStateKey]?.length ?? 0) -
        uploadQueue.current.filter(item => item.composerKey === composerStateKey).length,
    );
    const files = inputFiles.slice(0, capacity);
    if (files.length === 0) {
      toast.error(t('composer.attachment_limit', {
        count: MAX_ATTACHMENTS_PER_TURN,
        defaultValue: `Up to ${MAX_ATTACHMENTS_PER_TURN} attachments per message.`,
      }));
      return;
    }
    if (files.length < inputFiles.length) {
      toast.info(t('composer.attachment_limit_partial', {
        count: MAX_ATTACHMENTS_PER_TURN,
        defaultValue: `Only the first ${MAX_ATTACHMENTS_PER_TURN} attachments were added.`,
      }));
    }

    const prepared = preparedConversation.current?.chatId === chatId ? preparedConversation.current : null;
    const stageLocally = !!prepareConversation && !chatPersisted && !prepared;
    const batch: PendingUpload[] = files.map(file => ({
      id: crypto.randomUUID(), composerKey: composerStateKey, name: file.name,
      type: requestedType ?? inferredAttachmentType(file), file, chatId, scopeId: wfId,
      projectId: prepared?.projectId ?? projectId,
      controller: new AbortController(), status: stageLocally ? 'staged' : 'queued',
    }));
    uploadQueue.current.push(...batch);
    setUploads([...uploadQueue.current]);
    await processUploadQueue();
  }, [chatId, chatPersisted, composerStateKey, historyReady, projectId, prepareConversation, readOnly, t, wfId, processUploadQueue]);

  const handleFileInput = useCallback((
    event: ChangeEvent<HTMLInputElement>,
    type: ChatFileAttachmentType,
  ) => {
    const files = Array.from(event.currentTarget.files ?? []);
    event.currentTarget.value = '';
    void uploadFiles(files, type);
  }, [uploadFiles]);

  const handlePaste = useCallback((event: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(event.clipboardData.files ?? []);
    if (files.length === 0) return;
    event.preventDefault();
    void uploadFiles(files);
  }, [uploadFiles]);

  const handleDragEnter = useCallback((event: DragEvent<HTMLDivElement>) => {
    if (!Array.from(event.dataTransfer.types).includes('Files')) return;
    event.preventDefault();
    dragDepthRef.current += 1;
    setDragActive(true);
  }, []);

  const handleDragOver = useCallback((event: DragEvent<HTMLDivElement>) => {
    if (!Array.from(event.dataTransfer.types).includes('Files')) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
  }, []);

  const handleDragLeave = useCallback((event: DragEvent<HTMLDivElement>) => {
    if (!Array.from(event.dataTransfer.types).includes('Files')) return;
    event.preventDefault();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setDragActive(false);
  }, []);

  const handleDrop = useCallback((event: DragEvent<HTMLDivElement>) => {
    dragDepthRef.current = 0;
    setDragActive(false);
    const types = Array.from(event.dataTransfer.types);
    if (!types.includes('Files')) {
      // Native text drops retain their caret/selection behavior. URL/image
      // drags without bytes must not masquerade as successful file uploads.
      if (types.includes('text/uri-list')) {
        event.preventDefault();
        toast.info(t('composer.drop_link_hint', 'This drag contains a link, not a file. Paste the link into your message or download the file before attaching it.'));
      }
      return;
    }
    event.preventDefault();
    const items = Array.from(event.dataTransfer.items ?? []);
    if (items.some(item => item.webkitGetAsEntry?.()?.isDirectory)) {
      toast.error(t('composer.drop_directory', 'Folders cannot be attached directly. Select individual files or upload an archive.'));
      return;
    }
    const files = Array.from(event.dataTransfer.files ?? []);
    if (files.length === 0) {
      toast.error(t('composer.drop_empty', 'No readable file was provided by this drag. Please use the file picker.'));
      return;
    }
    void uploadFiles(files);
  }, [t, uploadFiles]);

  useEffect(() => {
    if (!composerStorageKey || !composerStateKey || !account) {
      hydratedComposerKeyRef.current = null;
      return;
    }
    try {
      const raw = discardReloadedDraft(composerStateKey) ? null : window.localStorage.getItem(composerStorageKey);
      const current = useChatStreamStore.getState();
      if (raw && !current.composerInputs[composerStateKey] && !current.pendingAttachments[composerStateKey]?.length) {
        const parsed = JSON.parse(raw) as { text?: unknown; attachments?: unknown };
        if (typeof parsed.text === 'string') {
          current.setComposerInput(composerStateKey, parsed.text.slice(0, 100_000));
        }
        if (Array.isArray(parsed.attachments)) {
          useChatStreamStore.setState((state) => ({
            pendingAttachments: {
              ...state.pendingAttachments,
              [composerStateKey]: parsed.attachments as Attachment[],
            },
          }));
        }
      }
    } catch {
      window.localStorage.removeItem(composerStorageKey);
    }
    hydratedComposerKeyRef.current = composerStateKey;
  }, [account, composerStateKey, composerStorageKey]);

  const draftSyncEnabled = historyReady && !readOnly && (chatPersisted || attachmentCreatedChat === chatId);
  const contextDraft = useContextDraft(composerStateKey, chatId, draftSyncEnabled);


  useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (!active) return;
      setNotice(null);
      setActiveIdx(0);
      setMenuDismissed(false);
    });
    return () => {
      active = false;
    };
  }, [chatId]);

  // A queued draft (e.g. an ErrorCard "Ask the agent to fix this") prefills
  // the textarea and focuses it — the user reviews/edits before sending. We
  // append to any in-progress text rather than clobbering it, then clear the
  // store slot so re-mounts don't re-apply it.
  useEffect(() => {
    if (draft == null) return;
    if (draft.chatId !== chatId) return;
    let active = true;
    queueMicrotask(() => {
      // Strict Mode can replay the effect before this microtask runs. Also
      // reject stale work if a different suggestion or conversation won.
      if (!active || useChatStreamStore.getState().draft !== draft) return;
      consumeDraft();
      setValue((prev) => (prev ? `${prev}\n\n${draft.text}` : draft.text));
    });
    return () => { active = false; };
  }, [chatId, draft, consumeDraft, setValue]);

  const runtimeUnavailableReason = showModelSelector
    && runtimeCapabilitiesQuery.isFetched
    && (
      runtimeCapabilitiesQuery.data?.runtime_available === false
      || runtimeCapabilitiesQuery.data?.authenticated === false
      || (runtimeCapabilitiesQuery.data?.models.length ?? 0) === 0
    )
    ? t(
        'composer.runtime_unavailable',
        'Connect an API or account for this runtime before sending a message.',
      )
    : null;
  const effectiveDisabledReason = disabledReason ?? runtimeUnavailableReason;
  const externallyDisabled = !!effectiveDisabledReason;
  const canRetry =
    streamBelongsToThisChat &&
    (streamState === 'cancelled' || streamState === 'failed' || streamState === 'interrupted') &&
    (!!lastInput?.content || !!lastInput?.attachments?.length || !!lastInput?.control) &&
    !readOnly &&
    !externallyDisabled;
  const canSend =
    !!chatId &&
    !draftPreparing &&
    !contextDraft?.conflict &&
    !updateProjectMcp.isPending &&
    (value.trim().length > 0 || pendingAttachments.length > 0 || activeUploads.some(item => item.status === 'staged')) &&
    activeUploads.every(item => item.status === 'staged') &&
    !isStreaming &&
    !readOnly &&
    historyReady &&
    !externallyDisabled;

  /**
   * Send one turn. Thin wrapper around `runAgentTurn` — kept here as a
   * named function so Retry / Send can both call it with the same
   * signature, and so the `if (!chatId) return` guard lives in one
   * place rather than at every caller.
   *
   * Attachments lifecycle (T15.5): the caller moves the chips out of the
   * composer at click-time. `runAgentTurn` projects that same snapshot into
   * the optimistic user message, while `lastInput.attachments` remains the
   * retry source of truth. A request rejected before durable acceptance is
   * the only path that restores the snapshot to the composer.
   */
  const doSend = async (
    content: string,
    attachments?: Attachment[],
    mode?: 'chat' | 'browser',
    approvalMode?: ApprovalMode,
    control?: HitlContinueControl,
    onAccepted?: () => void,
    skillUse?: SkillUseSelection,
  ) => {
    if (!chatId) return false;
    const origin = embedded && agentSurface === 'browser' ? extensionOrigin() : null;
    if (origin && window.parent !== window) {
      window.parent.postMessage({ type: 'BROWSER_TURN_ORIGIN', chatId }, origin);
    }
    return runAgentTurn({
      wfId,
      chatId,
      projectId: preparedConversation.current?.chatId === chatId
        ? preparedConversation.current.projectId : projectId,
      content,
      control,
      attachments,
      skillUse,
      mode,
      approvalMode,
      agentSettings: getChatAgentSettings(chatId),
      // Where the chat lives: the embed IS the extension side panel. The backend
      // uses this to gate the side-panel-only `/browser` command (main app → a
      // NOTICE telling the user to use the side panel).
      surface: embedded ? 'sidepanel' : 'main',
      agentSurface,
      onAccepted,
    });
  };

  const handleSend = async () => {
    if (!canSend || preparationRef.current) return;
    // The backend's atomic Chat claim is the sole authority for concurrent
    // Turns. A preflight active-turn GET used to block the optimistic bubble
    // and textarea clear on every send, while still being inherently racy.
    setNotice(null);
    // Embed (side-panel) browser surface: a BARE message in the browser-first
    // side panel is promoted to a `mode=browser` turn using the shell-provided
    // stable `browserId` used by the embedded control path. `browserId` present
    // ⇒ we are inside the side panel. Otherwise we are the MAIN-APP sidebar,
    // where the BACKEND owns command parsing and a `/browser` is refused with a
    // NOTICE (browser control is side-panel-only).
    let content: string;
    let mode: 'chat' | 'browser' | undefined;
    if (embedded && defaultMode === 'browser') {
      // A bare side-panel message is promoted to a browser turn.
      const parsed = parseAgentCommand(value.trim());
      mode = parsed.mode === 'chat' ? 'browser' : parsed.mode;
      // Send the RAW text (NOT parsed.content) so the BACKEND owns command
      // parsing — a leading `/browser` activates browser mode there (surface
      // "sidepanel" lets it through) AND the backend's empty-bubble fallback
      // shows the typed text. Stripping here made a bare "/browser" send "" → an
      // empty user bubble + no command for the backend to act on.
      content = value.trim();
    } else {
      // MAIN APP: send the raw text so the BACKEND owns command parsing
      // (`/workflow` → additive turn; `/browser` → refused with a NOTICE toast).
      content = value.trim();
      mode = undefined;
    }

    const skillUse = selectedSkill?.scope === composerStateKey ? { skill_id: selectedSkill.skill_id, name: selectedSkill.name }
      : lastInput?.content === content ? lastInput.skillUse : undefined;
    if (content.startsWith('/skill-use') && (!skillUse || !content.startsWith(`/skill-use:[${skillUse.name}] `))) {
      setSkillPickerOpen(true);
      return;
    }
    preparationRef.current = true;
    setDraftPreparing(true);
    // Show the submission before any save/upload request. Keep the underlying
    // draft intact until preparation succeeds, so conflicts and upload errors
    // cannot lose text or references. This is not yet a server-accepted Turn.
    const preparingChatId = chatId!;
    const clearPreparation = () => useChatStreamStore.setState(state => {
      const preparingMessages = { ...state.preparingMessages };
      delete preparingMessages[preparingChatId];
      return { preparingMessages };
    });
    useChatStreamStore.setState(state => ({ preparingMessages: {
      ...state.preparingMessages,
      [preparingChatId]: { role: 'user', content,
        attachments: composerStateKey ? state.pendingAttachments[composerStateKey] : undefined },
    } }));
    try {
      if (prepareConversation && chatId) {
        const preparedProjectId = await prepareConversation();
        if (currentConversation.current !== chatId) return;
        preparedConversation.current = { chatId, projectId: preparedProjectId };
        if (draftMcpIds !== null) {
          const selection = await fetchProjectMcpSelection(preparedProjectId);
          await updateProjectMcp.mutateAsync({ projectId: preparedProjectId,
            selection: { ...selection, mcp_server_ids: draftMcpIds } });
          setDraftMcpIds(null);
        }
        uploadQueue.current = uploadQueue.current.map(item => item.chatId === chatId && item.status === 'staged'
          ? { ...item, projectId: preparedProjectId, status: 'queued' } : item);
        await processUploadQueue();
        if (currentConversation.current !== chatId || uploadQueue.current.some(item => item.chatId === chatId)) return;
        if ((useChatStreamStore.getState().composerInputs[composerStateKey!] ?? '').trim() !== content) {
          toast.info(t('composer.context.changedBeforeSend', 'Your draft changed while syncing. Review it and send again.'));
          return;
        }
      }
      if (draftSyncEnabled && contextDraft) {
        setValue(content);
        await contextDraft.flush();
        const latest = composerStateKey ? useChatStreamStore.getState().composerInputs[composerStateKey] : content;
        if ((latest ?? '').trim() !== content) {
          toast.info(t('composer.context.changedBeforeSend', 'Your draft changed while syncing. Review it and send again.'));
          return;
        }
      }
    } catch (error) {
      toast.error(prepareConversation ? errorMessage(error)
        : t('composer.context.syncFailed', 'Could not sync the draft. Your message is preserved; retry before sending.'));
      return;
    } finally {
      preparationRef.current = false;
      setDraftPreparing(false);
      clearPreparation();
    }
    contextDraft?.beginSend();

    // Read pending attachments via getState so we always capture the
    // freshest value (the chips list may have shifted between renders
    // via a stale `pendingAttachments` selector snapshot).
    const attachments = composerStateKey
      ? useChatStreamStore.getState().pendingAttachments[composerStateKey] ?? []
      : [];
    optimisticSubmissionRef.current = true;
    // Commit the clear as its own synchronous visual state before switching
    // the empty-chat shell to the transcript. React otherwise batches both
    // updates and the newly-mounted conversation composer can paint one frame
    // with the old draft. The optimistic bubble is projected immediately
    // after this block, so the user sees one clean handoff: draft clears first,
    // then the same content appears in the transcript.
    flushSync(() => {
      setValue('');
      // Text and attachments belong to one message. Move both out of the
      // composer before awaiting the network so the optimistic user bubble is
      // the sole visible owner while the Agent is running.
      if (composerStateKey) {
        useChatStreamStore.getState().clearAttachments(composerStateKey);
      }
    });
    // The empty-chat shell and the conversation transcript are separate
    // render branches. Switch branches at click-time so the optimistic bubble
    // and thinking indicator appear during request setup, not after the
    // backend has accepted the Turn. Remove the durable composer snapshot
    // first: otherwise the newly-mounted conversation composer would hydrate
    // the just-sent text back into its textarea. The in-memory `content` and
    // `attachments` snapshots below remain the rollback source of truth.
    contextDraft?.save();
    onSendStart?.();
    let accepted = false;
    await doSend(
      content,
      attachments.length > 0 ? attachments : undefined,
      mode,
      useAgentSettingsStore.getState().approvalMode,
      undefined,
      () => {
        accepted = true;
        onSendAccepted?.();
        optimisticSubmissionRef.current = false;
        setAttachmentCreatedChat(chatId);
        void contextDraft?.finishSend(true).catch(() => undefined);
        void runtimeCapabilitiesQuery.refetch();
        if (projectId && agentSurface !== 'browser') {
          acceptProjectChatDraft(account, projectId, wfId, chatId as string);
        }
        useUIStore.getState().addOptimisticChatSession({
          scopeId: wfId,
          projectId,
          chat_id: chatId as string,
          chat_context: content.slice(0, 80),
          surface: agentSurface === 'browser' ? 'browser' : 'chat',
        });
      },
      content.startsWith('/skill-use:') ? skillUse : undefined,
    );
    if (!accepted) {
      // A request is sent only when the backend exposes durable acceptance.
      // A 409 race, pre-accept disconnect, or malformed success response must
      // never eat the user's text or attachments.
      optimisticSubmissionRef.current = false;
      if (draftSyncEnabled && contextDraft) {
        // A missing acceptance event does not prove the server rejected the
        // message. The durable draft controller owns conditional restoration.
        void contextDraft.finishSend(false).catch(() => undefined);
      } else {
        setValue((current) => current ? `${content}\n\n${current}` : content);
        if (composerStateKey) {
          const currentAttachments = useChatStreamStore.getState().pendingAttachments[composerStateKey] ?? [];
          const keys = new Set(attachments.map(contextAttachmentKey));
          useChatStreamStore.getState().setAttachments(composerStateKey,
            [...attachments, ...currentAttachments.filter(item => !keys.has(contextAttachmentKey(item)))]);
        }
        contextDraft?.discardSubmission();
      }
      textareaRef.current?.focus();
    }
  };

  const handleRetry = async () => {
    if (!canRetry || !lastInput) return;
    await doSend(
      lastInput.content,
      lastInput.attachments,
      lastInput.mode,
      lastInput.approvalMode ?? useAgentSettingsStore.getState().approvalMode,
      lastInput.control,
      undefined,
      lastInput.skillUse,
    );
  };

  const handleStop = () => {
    if (chatId) {
      void cancelActiveTurn(chatId);
    }
  };

  // ── Slash-command autocomplete ─────────────────────────────────────────
  // Active only while the input IS a command token being typed: starts with "/"
  // and has no whitespace yet (once a space is typed, the command is chosen and
  // the rest is its prompt). Candidates are filtered by case-insensitive prefix.
  const typingCommand =
    !readOnly && value.startsWith('/') && !/\s/.test(value);
  const candidates = typingCommand
    ? slashCommands.filter((c) => c.trigger.startsWith(value.toLowerCase()))
    : [];
  const menuOpen = !menuDismissed && candidates.length > 0;
  const active = Math.min(activeIdx, Math.max(candidates.length - 1, 0));
  const mentionQuery = !readOnly
    ? findAttachmentMention(value, caretPosition)
    : null;
  const mentionCandidates = mentionQuery
    ? fileAttachments.filter((attachment) =>
        attachment.name.toLocaleLowerCase().includes(
          mentionQuery.query.toLocaleLowerCase(),
        ),
      )
    : [];
  const mentionMenuOpen =
    !menuOpen && !mentionDismissed && !!mentionQuery && mentionCandidates.length > 0;
  const activeMention = Math.min(
    mentionActiveIdx,
    Math.max(mentionCandidates.length - 1, 0),
  );

  const completeCommand = (cmd: SlashCommand) => {
    setValue(`${cmd.trigger} `);
    if (cmd.trigger === '/goal') {
      setGoalPickerOpen(true);
      setMenuDismissed(true);
      return;
    }
    if (cmd.trigger === '/skill-use') {
      setSkillPickerOpen(true);
      setMenuDismissed(true);
      return;
    }
    setMenuDismissed(true);
    // The controlled re-render can reset the caret — refocus + move it to end.
    requestAnimationFrame(() => {
      const ta = textareaRef.current;
      if (ta) {
        ta.focus();
        const end = ta.value.length;
        ta.setSelectionRange(end, end);
      }
    });
  };

  const completeMention = (name: string) => {
    if (!mentionQuery) return;
    const next = insertAttachmentMention(value, mentionQuery, name);
    setValue(next.value);
    setMentionDismissed(true);
    requestAnimationFrame(() => {
      const textarea = textareaRef.current;
      if (!textarea) return;
      textarea.focus();
      textarea.setSelectionRange(next.caret, next.caret);
      setCaretPosition(next.caret);
    });
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (mentionMenuOpen) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setMentionActiveIdx((index) => (index + 1) % mentionCandidates.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setMentionActiveIdx(
          (index) => (index - 1 + mentionCandidates.length) % mentionCandidates.length,
        );
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setMentionDismissed(true);
        return;
      }
      const composing = e.nativeEvent.isComposing || e.keyCode === 229;
      if (!composing && (e.key === 'Tab' || e.key === 'Enter')) {
        e.preventDefault();
        completeMention(mentionCandidates[activeMention].name);
        return;
      }
    }
    // While the command menu is open it OWNS the navigation keys (VSCode-style).
    if (menuOpen) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setActiveIdx((i) => (i + 1) % candidates.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActiveIdx((i) => (i - 1 + candidates.length) % candidates.length);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setMenuDismissed(true);
        return;
      }
      const composing = e.nativeEvent.isComposing || e.keyCode === 229;
      if (
        !composing &&
        (e.key === 'Tab' ||
          (e.key === 'Enter' && !e.shiftKey && !e.metaKey && !e.ctrlKey))
      ) {
        e.preventDefault();
        completeCommand(candidates[active]);
        return;
      }
    }

    // ── send / newline ───────────────────────────────────────────────────
    if (e.key !== 'Enter') return;
    // Enter while an IME composition is active must never send.
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    // Shift+Enter is always a newline (the escape hatch).
    if (e.shiftKey) return;
    // ⌘/Ctrl+Enter sends; bare Enter falls through to the textarea = newline.
    if (e.metaKey || e.ctrlKey) {
      e.preventDefault();
      void handleSend();
    }
  };

  // Pick exactly one action button so Retry / Send / Stop never collide.
  // Order of precedence reflects user intent: a streaming turn must be
  // stoppable; a stopped/failed turn should be retryable; otherwise send.
  const hasNewDraft = value.trim().length > 0 || pendingAttachments.length > 0;
  const action = isStreaming ? 'stop' : canRetry && !hasNewDraft ? 'retry' : 'send';

  const compactButtonClass = embedded ? 'h-8 w-8 rounded-full p-0' : undefined;
  const inputTypographyClass = embedded
    ? 'px-3 py-2 text-[13px] leading-5'
    : quietFrame
      ? 'px-0 py-1.5 text-readable leading-6'
      : 'px-3 py-2 text-readable leading-6';
  const attachmentPickerDisabled =
    !chatId || isStreaming || readOnly || !historyReady || externallyDisabled || draftPreparing;
  const openAttachmentPicker = (type: ChatFileAttachmentType) => {
    const input = type === 'image'
      ? imageInputRef.current
      : type === 'video'
        ? videoInputRef.current
        : fileInputRef.current;
    input?.click();
  };

  return (
    <div className="flex flex-col gap-3">
      <GoalStatus key={chatId} goal={goalQuery.data?.goal} streaming={isStreaming} disabled={readOnly || externallyDisabled || !historyReady}
        onStop={handleStop} onCommand={command => doSend(command, undefined, undefined, useAgentSettingsStore.getState().approvalMode)} />
    <div
      data-role="chat-composer-frame"
      className={cn(
        'flex flex-col',
        framed && 'chat-composer-shell',
        embedded ? 'm-3 mt-1 gap-2 rounded-2xl border border-edge-subtle bg-surface-raised p-2 shadow-raised transition-shadow duration-150 focus-within:border-focus/40 focus-within:shadow-popover motion-reduce:transition-none' : 'gap-2',
        quietFrame ? 'p-3.5' : !embedded && 'p-3',
      )}
    >
      <div
        className="relative"
        onDragEnter={handleDragEnter}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        {goalPickerOpen && <div role="dialog" aria-label={t('goal.details')} className="absolute bottom-full left-0 right-0 z-50 mb-2 rounded-xl border bg-popover p-2 shadow-lg" onKeyDown={event => { if (event.key === 'Escape') { setGoalPickerOpen(false); textareaRef.current?.focus(); } }}>
          {(['new', 'resume', 'status', 'edit', 'clear'] as const).map(action => <button key={action} type="button" className="block w-full rounded-lg p-2 text-left text-sm hover:bg-accent" onClick={() => {
            setValue(action === 'new' ? '/goal ' : `/goal:${action} `); setGoalPickerOpen(false); setMenuDismissed(true);
            requestAnimationFrame(() => {
            const input = textareaRef.current;
            // A newly opened picker or another control owns focus now.
            if (document.activeElement !== document.body && document.activeElement !== input) return;
            input?.focus(); input?.setSelectionRange(input.value.length, input.value.length);
          });
          }}>{action === 'new' ? <><span className="block">{t('goal.noSubcommand')}</span><span className="block text-xs text-muted-foreground">{t('goal.enterObjective')}</span></> : t(`goal.${action}`)}</button>)}
        </div>}
        {skillPickerOpen && <SkillUsePicker onClose={() => { setSkillPickerOpen(false); setMenuDismissed(false); textareaRef.current?.focus(); }} onSelect={(skill) => {
          const task = value.replace(/^\/skill-use:\[[^\]]*\]\s*|^\/[^\s]*\s*/, '');
          setSelectedSkill({ skill_id: skill.id, name: skill.name, scope: composerStateKey });
          setValue(`/skill-use:[${skill.name}] ${task}`);
          setSkillPickerOpen(false);
          setMenuDismissed(true);
          requestAnimationFrame(() => {
            const input = textareaRef.current;
            // A newly opened picker or another control owns focus now.
            if (document.activeElement !== document.body && document.activeElement !== input) return;
            input?.focus(); input?.setSelectionRange(input.value.length, input.value.length);
          });
        }} />}
        {menuOpen && !skillPickerOpen && !goalPickerOpen && (
          <div
            className="absolute bottom-full left-0 right-0 z-50 mb-1 overflow-hidden rounded-md border bg-popover shadow-md"
            role="listbox"
            data-role="agent-slash-menu"
          >
            {candidates.map((c, i) => (
              <button
                key={c.trigger}
                type="button"
                role="option"
                aria-selected={i === active}
                // onMouseDown (not onClick) so the textarea doesn't blur first.
                onMouseDown={(e) => {
                  e.preventDefault();
                  completeCommand(c);
                }}
                onMouseEnter={() => setActiveIdx(i)}
                className={cn(
                  'flex w-full items-center gap-2 px-2 py-1.5 text-left text-xs',
                  i === active ? 'bg-accent' : 'hover:bg-accent/50',
                )}
                data-role="agent-slash-option"
              >
                <span className="font-mono font-semibold">{c.trigger}</span>
                <span className="truncate text-muted-foreground">
                  {t(c.descKey)}
                </span>
              </button>
            ))}
          </div>
        )}
        {mentionMenuOpen && (
          <div
            className="absolute bottom-full left-0 right-0 z-50 mb-1 max-h-56 overflow-y-auto rounded-lg border border-edge-structural bg-popover p-1 shadow-popover"
            role="listbox"
            aria-label={t('composer.attachment_mentions', 'Attached files')}
            data-role="agent-attachment-mention-menu"
          >
            {mentionCandidates.map((attachment, index) => (
              <button
                key={attachment.path}
                type="button"
                role="option"
                aria-selected={index === activeMention}
                onMouseDown={(event) => {
                  event.preventDefault();
                  completeMention(attachment.name);
                }}
                onMouseEnter={() => setMentionActiveIdx(index)}
                className={cn(
                  'flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm',
                  index === activeMention ? 'bg-accent' : 'hover:bg-accent/55',
                )}
                data-role="agent-attachment-mention-option"
              >
                <span className="text-base" aria-hidden="true">
                  {attachmentEmoji(attachment.type)}
                </span>
                <span className="min-w-0 flex-1 truncate font-medium">{attachment.name}</span>
                <span className="shrink-0 text-xs uppercase text-muted-foreground">
                  {attachment.type}
                </span>
              </button>
            ))}
          </div>
        )}
        <div
          className={cn(
            'relative overflow-hidden rounded-none transition-colors',
            (quietFrame || embedded)
              ? 'bg-transparent'
              : 'border border-edge-structural bg-surface-raised',
            dragActive && 'border-focus bg-focus/[0.035] ring-2 ring-focus/15',
          )}
          data-role="agent-composer-dropzone"
        >
          {draftSyncEnabled && (contextDraft?.error || contextDraft?.conflict) && (
            <div className="m-2 rounded-lg border border-state-warning/25 bg-state-warning/5 p-2 text-xs" role="status">
              <p>{contextDraft.conflict
                ? t('composer.context.conflict', 'The draft was edited in another window. Choose which text to keep; attachments are merged.')
                : t('composer.context.syncFailed', 'Could not sync the draft. Your message is preserved; retry before sending.')}</p>
              <div className="mt-1 flex flex-wrap gap-3">
                {contextDraft.conflict ? <>
                  <button type="button" className="font-medium underline" onClick={() => void contextDraft.chooseText(true).catch(() => undefined)}>{t('composer.context.keepLocal','Keep my text')}</button>
                  <button type="button" className="font-medium underline" onClick={() => void contextDraft.chooseText(false).catch(() => undefined)}>{t('composer.context.useRemote','Use saved text')}</button>
                </> : <button type="button" className="font-medium underline" onClick={() => void contextDraft.refresh().then(() => contextDraft.flush()).catch(() => undefined)}>{t('retry','Retry')}</button>}
              </div>
            </div>
          )}
          {(pendingAttachments.length > 0 || activeUploads.length > 0) && (
            <div
              className={cn(
                'flex min-w-0 w-full max-w-full flex-nowrap gap-2 overflow-x-auto overscroll-x-contain px-2.5 pt-2.5 pb-1',
                quietFrame && 'px-0 pt-0',
                compactAttachments && 'gap-1.5',
              )}
              tabIndex={0}
              aria-label={t('composer.attachment_mentions', 'Attached files')}
              data-role="agent-composer-attachments"
            >
              {(!draftPreparing ? pendingAttachments : []).map((attachment, index) => (
                <ContextAttachmentCard key={contextAttachmentKey(attachment)} attachment={attachment} originChatId={chatId} compact={compactAttachments}
                  onRemove={() => {
                    if (!composerStateKey) return;
                    removeAttachmentAt(composerStateKey,index);
                    if (draftSyncEnabled) void contextDraft?.remove(attachment).catch(() => undefined);
                  }} />
              ))}
              {activeUploads.map((upload) => (
                <span
                  key={upload.id}
                  className={cn("flex w-[164px] shrink-0 items-center gap-2 rounded-lg border border-edge-subtle bg-surface-sunken/45 px-2.5 text-xs opacity-80", compactAttachments ? "h-8" : "h-11")}
                  data-role="agent-composer-attachment-uploading"
                >
                  <span className="text-base" aria-hidden="true">{attachmentEmoji(upload.type)}</span>
                  <span className={cn("min-w-0 flex-1", compactAttachments && "flex items-center gap-1")} >
                    <span className="block truncate font-medium" title={upload.name}>{upload.name}</span>
                    <span className="flex items-center gap-1 text-xs text-muted-foreground">
                      {upload.status === 'uploading' ? <Loader2 className="h-2.5 w-2.5 animate-spin" /> : null}
                      <span title={upload.error}>{upload.status === 'failed' ? t('composer.uploadFailed', 'Upload failed')
                        : upload.status === 'queued' || upload.status === 'staged' ? t('composer.uploadQueued', 'Waiting…') : t('composer.uploading', 'Uploading…')}</span>
                    </span>
                  </span>
                  {upload.status === 'failed' ? <button type="button" className="shrink-0 rounded px-1 hover:bg-surface-hover"
                    aria-label={t('composer.retryUpload', 'Retry upload')} onClick={() => retryUpload(upload)}>↻</button> : null}
                  <button type="button" className="shrink-0 rounded px-1 hover:bg-surface-hover"
                    aria-label={t('composer.cancelUpload', 'Cancel upload')} onClick={() => cancelUpload(upload)}>×</button>
                </span>
              ))}
            </div>
          )}
          <Textarea
            ref={textareaRef}
            rows={embedded ? 3 : quietFrame ? 2 : 3}
            value={draftPreparing ? '' : value}
            onChange={(event) => {
              setValue(event.target.value);
              setCaretPosition(event.target.selectionStart ?? event.target.value.length);
              setMenuDismissed(false);
              setActiveIdx(0);
              setMentionDismissed(false);
              setMentionActiveIdx(0);
            }}
            onSelect={(event) => setCaretPosition(event.currentTarget.selectionStart)}
            onClick={(event) => setCaretPosition(event.currentTarget.selectionStart)}
            onPaste={handlePaste}
            onKeyDown={handleKeyDown}
            placeholder={
              readOnly
                ? t('composer.switch_to_latest', 'Switch to latest to edit')
                : effectiveDisabledReason
                  ? effectiveDisabledReason
                : !historyReady
                  ? t('composer.loading_history', 'Loading conversation…')
                  : isStreaming
                    ? t('composer.running_draft_placeholder', 'The Agent is working. Draft your next message…')
                  : chatId
                    ? t(
                        'composer.placeholder',
                        'Message the Agent. Type / to use a specific capability…',
                      )
                    : t('composer.select_chat', 'Select or start a chat')
            }
            disabled={!chatId || readOnly || !historyReady || externallyDisabled || draftPreparing}
            aria-label={t('composer.input_label', 'Message the agent')}
            className={cn(
              'resize-none rounded-none border-0 bg-transparent shadow-none focus-visible:ring-0 focus-visible:ring-offset-0 disabled:!bg-transparent disabled:!opacity-100 read-only:!bg-transparent',
              inputTypographyClass,
              embedded && 'min-h-[72px]',
              quietFrame && 'min-h-[64px] max-h-[138px] px-0',
            )}
            aria-autocomplete="list"
            data-role="agent-composer-input"
            data-context-chat={chatId ?? undefined}
            data-chat-id={chatId ?? undefined}
            data-history-ready={historyReady ? 'true' : 'false'}
          />
          {dragActive ? (
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center rounded-none bg-background/85 text-sm font-medium text-focus backdrop-blur-[1px]">
              <Paperclip className="mr-2 h-4 w-4" />
              {t('composer.drop_files', 'Drop files to attach')}
            </div>
          ) : null}
        </div>
      </div>
      {notice && (
        <p
          className="text-xs text-muted-foreground"
          data-role="agent-composer-notice"
          role="status"
        >
          {notice}
        </p>
      )}
      {effectiveDisabledReason && (
        <p
          className="rounded-md border border-border bg-muted/40 px-2 py-1.5 text-xs text-muted-foreground"
          data-role="agent-composer-disabled-reason"
        >
          {effectiveDisabledReason}
          {runtimeUnavailableReason && !disabledReason ? (
            <Link to="/settings?tab=runtime" className="ml-2 font-medium text-focus underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
              {t('composer.connectRuntime', 'Connect an account or API')}
            </Link>
          ) : null}
        </p>
      )}
      <div className="rounded-lg">
        <div
          className={cn(
            'flex items-center gap-2',
            showModelSelector ? 'justify-between' : 'justify-end',
            embedded && 'gap-1.5 px-0.5',
          )}
        >
          {showModelSelector ? (
            <div className="flex min-w-0 items-center gap-1">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className={cn(
                  'relative z-10 h-8 shrink-0 gap-1.5 px-2 text-xs font-normal text-muted-foreground hover:text-foreground',
                  optionsOpen && [
                    'rounded-b-none border border-b-0 border-edge-subtle bg-surface-sunken text-foreground',
                    'hover:bg-surface-sunken',
                    'after:absolute after:-bottom-0.5 after:left-[-1px] after:h-0.5 after:w-[calc(100%+2px)] after:bg-surface-sunken',
                  ],
                )}
                aria-label={t('composer.options.label', 'Turn options')}
                aria-expanded={optionsOpen}
                aria-controls="chat-composer-options"
                onClick={() => setOptionsOpen((open) => !open)}
                data-role="chat-composer-options-toggle"
              >
                <SlidersHorizontal className="h-3.5 w-3.5" />
                <span className="hidden sm:inline">{t('composer.options.label', 'Options')}</span>
                {effectiveSelectedMcpIds.length > 0 ? (
                  <span className="rounded-full bg-focus/10 px-1.5 text-xs font-medium text-focus">
                    {effectiveSelectedMcpIds.length}
                  </span>
                ) : null}
              </Button>
              <ComposerRuntimeSelectors
                capabilities={runtimeCapabilitiesQuery.data}
                loading={runtimeCapabilitiesQuery.isLoading}
                settings={chatAgentSettings?.settings}
                onChange={updateChatAgentSettings}
                disabled={isStreaming}
              />
              <ComposerAttachmentPicker
                disabled={attachmentPickerDisabled}
                onPick={openAttachmentPicker}
              />
            </div>
          ) : null}
          <div className={cn('flex items-center gap-2', embedded && 'gap-1.5')}>
            {!showModelSelector ? (
              <ComposerAttachmentPicker
                disabled={attachmentPickerDisabled}
                onPick={openAttachmentPicker}
              />
            ) : null}
            {action === 'stop' && (
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={handleStop}
                aria-label={t('stop', 'Stop')}
                title={t('stop', 'Stop')}
                className={compactButtonClass}
                data-action="agent-composer-stop"
              >
                <Square className="h-3.5 w-3.5" />
                {!embedded && t('stop', 'Stop')}
              </Button>
            )}
            {action === 'retry' && (
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => void handleRetry()}
                aria-label={t('retry', 'Retry')}
                title={t('retry', 'Retry')}
                className={compactButtonClass}
                data-action="agent-composer-retry"
              >
                <RotateCcw className="h-3.5 w-3.5" />
                {!embedded && t('retry', 'Retry')}
              </Button>
            )}
            {action === 'send' && (
              <Button
                type="button"
                size="sm"
                onClick={() => void handleSend()}
                disabled={!canSend}
                aria-label={t('chat.send', 'Send')}
                title={t('chat.send', 'Send')}
                className={compactButtonClass}
                data-action="agent-composer-send"
              >
                <Send className="h-3.5 w-3.5" />
                {!embedded && t('chat.send', 'Send')}
              </Button>
            )}
          </div>
        </div>
        {showModelSelector && optionsOpen && !compactOptions ? (
          <div
            id="chat-composer-options"
            className="-mt-px flex animate-in flex-wrap items-center gap-1 rounded-lg rounded-tl-none border border-edge-subtle bg-surface-sunken px-2 py-2 shadow-[0_8px_24px_-20px_hsl(var(--content-primary)/0.45)] fade-in-0 slide-in-from-top-1 duration-200 motion-reduce:animate-none"
            aria-label={t('composer.options.label', 'Turn options')}
            data-role="chat-composer-options"
          >
            <span className="px-1.5 text-xs font-medium text-content-tertiary">
              {t('composer.options.nextTurn', 'Next turn')}
            </span>
            <InlineReasoningEffortPicker
              capabilities={runtimeCapabilitiesQuery.data}
              settings={chatAgentSettings?.settings}
              onChange={updateChatAgentSettings}
              disabled={isStreaming}
            />
            <ComposerMcpPicker
              servers={mcpServersQuery.data ?? []}
              selectedIds={effectiveSelectedMcpIds}
              onChange={setSelectedMcpIds}
              disabled={isStreaming || (!projectMcpQuery.isSuccess && !prepareConversation) || updateProjectMcp.isPending}
              runtimeType={runtimeCapabilitiesQuery.data?.runtime_type}
            />
            <ApprovalModePicker disabled={isStreaming} />
          </div>
        ) : null}
      </div>
      {showModelSelector ? (
        <Sheet
          open={optionsOpen && compactOptions}
          onOpenChange={(open) => setOptionsOpen(open)}
        >
          <SheetContent
            side="bottom"
            className="max-h-[78dvh] rounded-t-xl px-4 pb-[max(1rem,env(safe-area-inset-bottom))] pt-5"
            data-role="chat-composer-options-sheet"
          >
            <SheetHeader className="pr-10 text-left">
              <SheetTitle className="text-base">
                {t('composer.options.label', 'Turn options')}
              </SheetTitle>
              <SheetDescription>
                {t('composer.options.mobileHint', 'Configure the next turn without leaving the conversation.')}
              </SheetDescription>
            </SheetHeader>
            <div className="mt-4 flex flex-col items-stretch gap-2 [&>button]:w-full [&_[role=combobox]]:w-full">
              <InlineReasoningEffortPicker
                capabilities={runtimeCapabilitiesQuery.data}
                settings={chatAgentSettings?.settings}
                onChange={updateChatAgentSettings}
                disabled={isStreaming}
              />
              <ComposerMcpPicker
                servers={mcpServersQuery.data ?? []}
                selectedIds={effectiveSelectedMcpIds}
                onChange={setSelectedMcpIds}
                disabled={isStreaming || (!projectMcpQuery.isSuccess && !prepareConversation) || updateProjectMcp.isPending}
                runtimeType={runtimeCapabilitiesQuery.data?.runtime_type}
              />
              <ApprovalModePicker disabled={isStreaming} />
            </div>
          </SheetContent>
        </Sheet>
      ) : null}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        className="sr-only"
        tabIndex={-1}
        onChange={(event) => handleFileInput(event, 'file')}
        aria-label={t('composer.attach_file', 'Attach file')}
        data-role="agent-composer-file-input"
      />
      <input
        ref={imageInputRef}
        type="file"
        accept="image/*"
        multiple
        className="sr-only"
        tabIndex={-1}
        onChange={(event) => handleFileInput(event, 'image')}
        aria-label={t('composer.attach_image', 'Attach image')}
        data-role="agent-composer-image-input"
      />
      <input
        ref={videoInputRef}
        type="file"
        accept="video/*"
        multiple
        className="sr-only"
        tabIndex={-1}
        onChange={(event) => handleFileInput(event, 'video')}
        aria-label={t('composer.attach_video', 'Attach video')}
        data-role="agent-composer-video-input"
      />
    </div>
    </div>
  );
}

function ComposerMcpPicker({
  servers,
  selectedIds,
  onChange,
  disabled,
  runtimeType,
}: {
  servers: import('@/lib/api/mcp-servers').McpServer[];
  selectedIds: string[];
  onChange: (ids: string[]) => void;
  disabled: boolean;
  runtimeType?: string;
}) {
  const { t } = useTranslation();
  const candidates = servers.filter((server) =>
    server.enabled
    && ['not_required', 'connected'].includes(server.connection_status)
    && !(runtimeType === 'codex' && server.transport === 'sse'),
  );
  const selected = new Set(selectedIds);
  const candidateIds = new Set(candidates.map((server) => server.id));
  const unavailableIds = selectedIds.filter((id) => !candidateIds.has(id));
  const toggle = (id: string, checked: boolean) => {
    onChange(
      checked
        ? [...selectedIds, id].filter((value, index, all) => all.indexOf(value) === index)
        : selectedIds.filter((value) => value !== id),
    );
  };
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={disabled}
          className="h-7 max-w-32 gap-1.5 rounded-md px-2 text-xs font-normal text-muted-foreground hover:text-foreground"
          aria-label={t('composer.mcp.label', 'MCP servers')}
          title={t('composer.mcp.hint', 'MCP settings are shared by all chats in this project')}
          data-role="chat-mcp-picker"
        >
          <Blocks className="h-3.5 w-3.5" />
          <span className="truncate">
            {selectedIds.length > 0
              ? t('composer.mcp.selected', { count: selectedIds.length, defaultValue: '{{count}} MCP' })
              : t('composer.mcp.none', 'MCP')}
          </span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" side="top" className="max-h-72 w-64 overflow-y-auto">
        <p className="px-2 py-1.5 text-xs text-muted-foreground">
          {t('composer.mcp.sharedHint', 'Saved for every chat in this project. Changes apply to new turns.')}
        </p>
        {candidates.length === 0 ? (
          <DropdownMenuItem disabled>
            {t('composer.mcp.empty', 'No compatible MCP servers installed')}
          </DropdownMenuItem>
        ) : candidates.map((server) => (
          <DropdownMenuCheckboxItem
            key={server.id}
            disabled={disabled}
            checked={selected.has(server.id)}
            onCheckedChange={(checked) => toggle(server.id, checked === true)}
            onSelect={(event) => event.preventDefault()}
            title={server.description ? `${server.name}\n${server.description}` : server.name}
          >
            <span className="min-w-0 flex-1 overflow-hidden">
              <span className="block truncate">{server.name}</span>
              {server.description ? (
                <span className="block truncate text-xs text-muted-foreground">
                  {server.description}
                </span>
              ) : null}
            </span>
          </DropdownMenuCheckboxItem>
        ))}
        {unavailableIds.map((id) => (
          <DropdownMenuCheckboxItem
            key={id}
            disabled={disabled}
            checked
            onCheckedChange={(checked) => toggle(id, checked === true)}
            onSelect={(event) => event.preventDefault()}
            className="text-destructive"
            title={`${t('composer.mcp.unavailable', 'Unavailable MCP')} · ${id}`}
          >
            <span className="min-w-0 flex-1 truncate">
              {t('composer.mcp.unavailable', 'Unavailable MCP')} · {id.slice(0, 8)}
            </span>
          </DropdownMenuCheckboxItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function ComposerAttachmentPicker({
  disabled,
  onPick,
}: {
  disabled: boolean;
  onPick: (type: ChatFileAttachmentType) => void;
}) {
  const { t } = useTranslation();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          disabled={disabled}
          className="h-8 w-8 rounded-md text-muted-foreground hover:bg-muted/45 hover:text-foreground"
          aria-label={t('composer.add_attachment', 'Add attachment')}
          title={t('composer.add_attachment', 'Add attachment')}
          data-action="agent-composer-attachment-menu"
        >
          <Paperclip className="h-4 w-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" side="top" className="w-44">
        <DropdownMenuItem onSelect={() => onPick('file')}>
          <FileText className="h-4 w-4" />
          {t('composer.attach_file', 'File')}
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => onPick('image')}>
          <Image className="h-4 w-4" />
          {t('composer.attach_image', 'Image')}
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => onPick('video')}>
          <Video className="h-4 w-4" />
          {t('composer.attach_video', 'Video')}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function isModelIdFromDifferentRuntime(
  modelId: string | null,
  runtimeType: AgentRuntimeCapabilities['runtime_type'],
): boolean {
  if (!modelId) return false;
  const prefix = modelId.split(':', 1)[0];
  return Boolean(prefix) && prefix !== runtimeType;
}

function ComposerRuntimeSelectors({
  capabilities,
  loading,
  settings,
  onChange,
  disabled,
}: {
  capabilities?: AgentRuntimeCapabilities;
  loading: boolean;
  settings?: AgentSettings;
  onChange: (patch: Partial<AgentSettings>) => void;
  disabled?: boolean;
}) {
  const modelId = settings?.modelId ?? null;
  const effort = settings?.reasoningEffort ?? null;
  useEffect(() => {
    if (!capabilities || !settings) return;
    if (capabilities.runtime_type === 'codex') {
      if (
        settings.temperature != null ||
        settings.maxTokens != null ||
        settings.timeout != null
      ) {
        // Codex app-server owns its model configuration surface. Do not carry
        // generation knobs from another Runtime into a Codex Turn.
        onChange({ temperature: null, maxTokens: null, timeout: null });
      }
    }
    if (isModelIdFromDifferentRuntime(modelId, capabilities.runtime_type)) {
      const catalogDefaultModelId = capabilities.models.some(
        (model) => model.id === capabilities.default_model_id,
      )
        ? capabilities.default_model_id
        : null;
      // A saved default from another Runtime must never leak into a fresh
      // draft. Same-Runtime unavailable credentials remain visible so the
      // user can repair them explicitly.
      onChange({ modelId: catalogDefaultModelId, reasoningEffort: null });
      return;
    }
    const selectedModel = capabilities.models.find(
      (model) => model.id === (modelId ?? capabilities.default_model_id),
    );
    if (!selectedModel) {
      // Preserve an explicit stale selection so the backend can reject it.
      // Clearing it here would silently switch the Chat to another credential.
      return;
    }
    if (modelId === null && capabilities.default_model_id) {
      // Materialize the catalog's concrete model id in the draft settings.
      // The composer therefore displays and sends the actual user/platform
      // selection instead of an ambiguous synthetic "Default" option.
      onChange({ modelId: capabilities.default_model_id });
      return;
    }
    if (
      effort &&
      !selectedModel.supported_reasoning_efforts.some((option) => option.id === effort)
    ) {
      onChange({ reasoningEffort: null });
    }
  }, [capabilities, effort, modelId, onChange, settings]);
  return (
    <RuntimeModelPicker
      capabilities={capabilities}
      loading={loading}
      settings={settings}
      onChange={onChange}
      disabled={disabled}
    />
  );
}

const DEFAULT_REASONING_EFFORT = '__runtime_default__';

function ApprovalModePicker({ disabled }: { disabled: boolean }) {
  const { t } = useTranslation();
  const mode = useAgentSettingsStore((state) => state.approvalMode);
  const setMode = useAgentSettingsStore((state) => state.setApprovalMode);
  return (
    <Select value={mode} onValueChange={(value) => setMode(value as ApprovalMode)} disabled={disabled}>
      <SelectTrigger
        aria-label={t('approval_mode.label', 'Authorization')}
        title={t('approval_mode.scope_hint', 'Controls pre-tool approval only; interactive requests still wait for you.')}
        className="h-8 w-[140px] min-w-0 rounded-md border-0 bg-transparent px-2 text-xs shadow-none hover:bg-muted/45 disabled:bg-transparent [&>span]:truncate"
        data-role="chat-approval-mode-select"
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent align="start">
        <SelectItem value="agent">{t('approval_mode.agent', 'Smart approval')}</SelectItem>
        <SelectItem value="always_ask">{t('approval_mode.always_ask', 'Always ask')}</SelectItem>
        <SelectItem value="always_allow">{t('approval_mode.always_allow', 'Always allow')}</SelectItem>
      </SelectContent>
    </Select>
  );
}

function InlineReasoningEffortPicker({
  capabilities,
  settings,
  onChange,
  disabled,
}: {
  capabilities?: AgentRuntimeCapabilities;
  settings?: AgentSettings;
  onChange: (patch: Partial<AgentSettings>) => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  const effort = settings?.reasoningEffort ?? null;
  const modelId = settings?.modelId ?? null;
  const model = capabilities?.models.find(
    (option) => option.id === (modelId ?? capabilities.default_model_id),
  );
  const efforts = model?.supported_reasoning_efforts ?? [];
  const selectedUnsupported = !!effort && !efforts.some((option) => option.id === effort);
  const selectedValue = selectedUnsupported ? DEFAULT_REASONING_EFFORT : effort ?? DEFAULT_REASONING_EFFORT;
  const defaultEffortLabel = t('reasoning_effort.default', 'Runtime default');
  const selectedEffort = efforts.find((option) => option.id === effort);
  const selectedEffortLabel = selectedEffort
    ? t(`reasoning_effort.${selectedEffort.id}`, selectedEffort.label)
    : defaultEffortLabel;

  return (
    <Select
      value={selectedValue}
      onValueChange={(next) => {
        onChange({
          reasoningEffort:
            next === DEFAULT_REASONING_EFFORT ? null : (next as ReasoningEffort),
        });
      }}
      disabled={disabled || efforts.length === 0}
    >
      <SelectTrigger
        aria-label={t('reasoning_effort.label', 'Thinking')}
        title={selectedEffortLabel}
        className="h-8 w-[104px] min-w-0 gap-1.5 rounded-md border-0 bg-transparent px-2 text-xs shadow-none hover:bg-muted/45 focus:ring-0 disabled:bg-transparent sm:w-[120px] [&>span]:truncate"
        data-role="chat-reasoning-effort-select"
      >
        <BrainCircuit className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <SelectValue placeholder={t('reasoning_effort.label', 'Thinking')} />
      </SelectTrigger>
      <SelectContent align="start">
        <SelectItem value={DEFAULT_REASONING_EFFORT} title={defaultEffortLabel}>
          {defaultEffortLabel}
        </SelectItem>
        {efforts.map((option) => (
          <SelectItem
            key={option.id}
            value={option.id}
            title={t(`reasoning_effort.${option.id}`, option.label)}
          >
            {t(
              `reasoning_effort.${option.id}`,
              option.label,
            )}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
