import { CONTEXT_DRAFT_RESET_EVENT } from '@/lib/chat/context-draft';
import { queryClient } from '@/app/query-client';
import { clearActiveTurn } from '@/lib/api/sse/active-turn';
import { useAgentSettingsStore } from '@/stores/agent-settings';
import { useChatStreamStore } from '@/stores/chat-stream';
import { useExecStreamStore } from '@/stores/exec-stream';
import { useUIStore } from '@/stores/ui';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import { clearRecentChatSelections } from '@/lib/chat/state-key';
import { clearProjectDraftMemory } from '@/lib/chat/project-draft';

/**
 * Clear client-side state that is scoped to the currently authenticated user.
 *
 * This is intentionally broader than invalidating chat queries. The app is a
 * single-page runtime: switching accounts without a hard refresh otherwise
 * leaves TanStack Query data, active chat ids, optimistic sessions, active-turn
 * cursors, and selected credential ids in memory.
 */
function resetRuntimeScopedClientState(): void {
  if (typeof window !== 'undefined') window.dispatchEvent(new Event(CONTEXT_DRAFT_RESET_EVENT));
  queryClient.clear();
  clearRecentChatSelections();
  clearProjectDraftMemory();
  clearActiveTurn();
  useChatStreamStore.getState().reset();
  useExecStreamStore.getState().reset();
  useWorkflowEditStore.getState().setDraft(null);
  useAgentSettingsStore.getState().reset();
  useUIStore.setState({
    lastActiveWorkflowId: null,
    activeProjectId: null,
    activeChatIds: { chat: null, browser: null },
    chatEntryIntent: null,
    optimisticChatSessions: [],
    chatScrollPositions: {},
    chatToolExpansion: {},
    chatViewStates: {},
    explorerOpen: false,
    canvasReadOnly: false,
    canvasInteracting: false,
    inspectorScope: 'auto',
    inspectorTab: 'node',
  });
}

export function resetAuthScopedClientState(): void {
  resetRuntimeScopedClientState();
}

/**
 * Drop every cache/cursor whose authorization boundary is the active
 * organization. This deliberately shares the same runtime cleanup as logout:
 * keeping an old SSE cursor, optimistic chat, or workflow draft across an
 * organization switch could momentarily expose stale data or attach a stream
 * with the previous Session generation.
 */
export function resetOrganizationScopedClientState(): void {
  resetRuntimeScopedClientState();
}
