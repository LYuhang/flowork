/**
 * Minimal UI state store.
 *
 * Holds cross-cutting UI concerns that survive route changes but are not
 * server state (server state lives in TanStack Query) and not workflow
 * graph state (that lives in route-local stores added by later tasks).
 *
 * `subscribeWithSelector` lets non-React callers (e.g. the error boundary)
 * subscribe to slice changes without re-rendering everything.
 */
import { create } from 'zustand';
import { subscribeWithSelector } from 'zustand/middleware';
import type { ChatViewState } from '@/lib/chat/preview-state';

const MAX_ERROR_LOG_ENTRIES = 50;

export interface UIErrorEntry {
  ts: number;
  message: string;
}

export interface UIState {
  lastActiveWorkflowId: string | null;
  activeProjectId: string | null;
  activeChatIds: Record<'chat' | 'browser', string | null>;
  chatEntryIntent: 'default' | 'select' | null;
  optimisticChatSessions: Array<{
    scopeId: string;
    projectId?: string | null;
    chat_id: string;
    chat_context: string;
    surface: 'chat' | 'browser';
    created_at: string;
  }>;
  removeOptimisticChatSession: (scopeId: string, chatId: string) => void;
  chatScrollPositions: Record<string, { top: number; stickToBottom: boolean }>;
  chatToolExpansion: Record<string, boolean>;
  chatViewStates: Record<string, ChatViewState>;
  commandPaletteOpen: boolean;
  agentSidebarWidth: number;
  errorLog: UIErrorEntry[];
  explorerOpen: boolean;      // lifted from CanvasPage local state
  // Top-level management shell: the left nav sidebar (Workflows / Tasks /
  // Deployments) collapse state. Mirrors `explorerOpen` semantics but for the
  // MANAGEMENT routes only (the nav sidebar never renders inside a workflow).
  // Collapsed → slim icon-only rail; expanded → icon + i18n label.
  navSidebarCollapsed: boolean;
  // #12: right Inspector open/collapse, driven by a toolbar toggle button
  // (mirrors `explorerOpen`). Default open so a freshly-loaded canvas still
  // shows the contextual inspector; the toolbar toggle + the in-panel
  // collapse/expand chevrons all flip this one slice.
  inspectorOpen: boolean;
  // Stream 0d: the active canvas is a pinned/read-only version. EVERY
  // mutation entry point (window-level keyboard handlers, undo/redo) must
  // early-return when true. `CanvasPage` mirrors its `readOnly` here.
  canvasReadOnly: boolean;
  setCanvasReadOnly: (readOnly: boolean) => void;

  // ── Node hover-card suppression ────────────────────────────────────────
  // True while the user is mid-gesture on the canvas — dragging a node or
  // drawing an edge (Canvas wires `onConnectStart/End` + the node `dragging`
  // prop). NodeHoverCard reads this and refuses to open so a peek card never
  // pops up mid-drag or edge connection. This is separate from inspector
  // machine above — do not conflate.
  canvasInteracting: boolean;
  setCanvasInteracting: (interacting: boolean) => void;

  // ── Inspector scope/tab state machine ──────────────────────────────────
  //
  // The Inspector is CONTEXTUAL by selection scope. `inspectorScope`:
  //   - `'auto'`     — scope derives from the live xyflow selection: a node
  //                    selected → node scope; nothing selected → workflow scope.
  //   - `'workflow'` — an explicit override (toolbar Execute / Run Batch) that
  //                    forces workflow scope REGARDLESS of selection. The
  //                    override callers also DESELECT the node so the two
  //                    sources of truth can't contradict; the override is
  //                    cleared back to `'auto'` the moment a node is selected.
  //
  // `inspectorTab` is the requested active tab; RightInspector keeps the
  // PER-SCOPE last-tab memory and reconciles `inspectorTab` against the tab
  // set valid for the resolved scope.
  inspectorScope: 'auto' | 'workflow';
  inspectorTab: string;
  /** Set scope + tab in one shot (toolbar Execute/Run Batch, run-start auto-focus). */
  requestInspectorTab: (scope: 'auto' | 'workflow', tab: string) => void;
  // A monotonically-incrementing signal: anything that wants to open the
  // workflow Check dialog bumps this (palette `check` action, future
  // surfaces). RightInspector/CanvasToolbar subscribe and open the dialog on
  // change. A counter (not a boolean) so repeated requests always re-fire.
  checkRequestId: number;
  requestCheck: () => void;
  setExplorerOpen: (open: boolean) => void;
  toggleExplorer: () => void;
  setNavSidebarCollapsed: (collapsed: boolean) => void;
  toggleNavSidebar: () => void;
  setInspectorOpen: (open: boolean) => void;
  toggleInspector: () => void;
  setLastActiveWorkflowId: (id: string | null) => void;
  setActiveProjectId: (id: string | null) => void;
  setActiveChatId: (surface: 'chat' | 'browser', id: string | null) => void;
  setChatEntryIntent: (intent: 'default' | 'select' | null) => void;
  addOptimisticChatSession: (item: {
    scopeId: string;
    projectId?: string | null;
    chat_id: string;
    chat_context: string;
    surface?: 'chat' | 'browser';
  }) => void;
  setChatScrollPosition: (key: string, position: { top: number; stickToBottom: boolean }) => void;
  setChatToolExpanded: (key: string, expanded: boolean) => void;
  setChatViewState: (
    key: string,
    update: Partial<ChatViewState> | ((current: ChatViewState) => Partial<ChatViewState>),
  ) => void;
  setCommandPaletteOpen: (open: boolean) => void;
  setAgentSidebarWidth: (width: number) => void;
  logError: (message: string) => void;
}

export const useUIStore = create<UIState>()(
  subscribeWithSelector((set) => ({
    lastActiveWorkflowId: null,
    activeProjectId: null,
    activeChatIds: { chat: null, browser: null },
    chatEntryIntent: null,
    optimisticChatSessions: [],
    chatScrollPositions: {},
    chatToolExpansion: {},
    chatViewStates: {},
    commandPaletteOpen: false,
    agentSidebarWidth: 400,
    errorLog: [],
    explorerOpen: false,
    navSidebarCollapsed: false,
    inspectorOpen: true,
    canvasReadOnly: false,
    canvasInteracting: false,
    inspectorScope: 'auto',
    inspectorTab: 'node',
    checkRequestId: 0,

    requestInspectorTab: (scope, tab) =>
      set({ inspectorScope: scope, inspectorTab: tab }),
    requestCheck: () => set((s) => ({ checkRequestId: s.checkRequestId + 1 })),

    setCanvasReadOnly: (readOnly) => set({ canvasReadOnly: readOnly }),
    setCanvasInteracting: (interacting) =>
      set({ canvasInteracting: interacting }),
    setExplorerOpen: (open) => set({ explorerOpen: open }),
    toggleExplorer: () => set((s) => ({ explorerOpen: !s.explorerOpen })),
    setNavSidebarCollapsed: (collapsed) =>
      set({ navSidebarCollapsed: collapsed }),
    toggleNavSidebar: () =>
      set((s) => ({ navSidebarCollapsed: !s.navSidebarCollapsed })),
    setInspectorOpen: (open) => set({ inspectorOpen: open }),
    toggleInspector: () => set((s) => ({ inspectorOpen: !s.inspectorOpen })),
    setLastActiveWorkflowId: (id) => set({ lastActiveWorkflowId: id }),
    setActiveProjectId: (id) => set({ activeProjectId: id }),
    setActiveChatId: (surface, id) =>
      set((state) => ({ activeChatIds: { ...state.activeChatIds, [surface]: id } })),
    setChatEntryIntent: (intent) => set({ chatEntryIntent: intent }),
    addOptimisticChatSession: (item) =>
      set((s) => {
        const next = {
          scopeId: item.scopeId,
          projectId: item.projectId ?? null,
          chat_id: item.chat_id,
          chat_context: item.chat_context,
          surface: item.surface ?? 'chat',
          created_at: new Date().toISOString(),
        };
        return {
          optimisticChatSessions: [
            next,
            ...s.optimisticChatSessions.filter(
              (old) => !(old.scopeId === next.scopeId && old.chat_id === next.chat_id),
            ),
          ].slice(0, 50),
        };
      }),
    removeOptimisticChatSession: (scopeId, chatId) =>
      set((s) => ({
        optimisticChatSessions: s.optimisticChatSessions.filter(
          (item) => !(item.scopeId === scopeId && item.chat_id === chatId),
        ),
      })),
    setChatScrollPosition: (key, position) =>
      set((state) => {
        const next = { ...state.chatScrollPositions };
        delete next[key];
        next[key] = position;
        const entries = Object.entries(next);
        return { chatScrollPositions: Object.fromEntries(entries.slice(-100)) };
      }),
    setChatToolExpanded: (key, expanded) =>
      set((state) => {
        const next = { ...state.chatToolExpansion };
        delete next[key];
        next[key] = expanded;
        const entries = Object.entries(next);
        return { chatToolExpansion: Object.fromEntries(entries.slice(-500)) };
      }),
    setChatViewState: (key, update) =>
      set((state) => {
        const current = state.chatViewStates[key] ?? {
          explorerOpen: false,
          debugOpen: false,
          previewOpen: false,
          todoCollapsed: false,
          activePreviewId: null,
          previewItems: [],
        };
        const next = { ...current, ...(typeof update === 'function' ? update(current) : update) };
        const entries = Object.entries({ ...state.chatViewStates, [key]: next });
        return { chatViewStates: Object.fromEntries(entries.slice(-100)) };
      }),
    setCommandPaletteOpen: (open) => set({ commandPaletteOpen: open }),
    setAgentSidebarWidth: (width) => set({ agentSidebarWidth: width }),
    logError: (message) =>
      set((state) => {
        const next = [...state.errorLog, { ts: Date.now(), message }];
        // Cap to last MAX_ERROR_LOG_ENTRIES entries (keep tail).
        if (next.length > MAX_ERROR_LOG_ENTRIES) {
          next.splice(0, next.length - MAX_ERROR_LOG_ENTRIES);
        }
        return { errorLog: next };
      }),
  })),
);
