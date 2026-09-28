import { useEffect, useRef } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { getApiBase } from '@/lib/base-path';
import { useAuthStore } from '@/stores/auth';
import { useChatStreamStore } from '@/stores/chat-stream';

export interface DebugMessage {
  cursor: number;
  id: string;
  role: string;
  content: string;
  turn_id: string | null;
  ts: number;
  tool_calls: Array<Record<string, unknown>>;
  tool_call_id?: string | null;
  attachments: unknown[];
  artifact?: Record<string, unknown> | null;
  invocation?: Record<string, unknown> | null;
  activity?: Record<string, unknown> | null;
  control?: Record<string, unknown> | null;
  visibility?: string;
  message_type?: string;
  meta: Record<string, unknown>;
}
export interface DebugTranscript {
  chat_id: string;
  messages: DebugMessage[];
  artifacts: Array<Record<string, unknown>>;
  turns: Array<Record<string, unknown>>;
  next_cursor: number;
}
interface DebugPage extends Omit<DebugTranscript, 'artifacts' | 'turns'> {
  artifacts: DebugTranscript['artifacts'] | null;
  turns: DebugTranscript['turns'] | null;
  has_more: boolean;
}

export async function readDebugIncrement(chatId: string, previous: DebugTranscript | undefined, signal: AbortSignal): Promise<DebugTranscript> {
  const baseline = previous?.chat_id === chatId ? previous : undefined;
  let cursor = baseline?.next_cursor ?? 0;
  const added: DebugMessage[] = [];
  let artifacts = baseline?.artifacts ?? [];
  let turns = baseline?.turns ?? [];
  let first = true;
  do {
    const token = useAuthStore.getState().token;
    const response = await fetch(`${getApiBase()}/api/v1/chats/${encodeURIComponent(chatId)}/debug/messages?after_id=${cursor}&limit=100&include_artifacts=${first}`, {
      signal, headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (response.status === 401) useAuthStore.getState().handle401();
    if (!response.ok) throw new Error(`Chat debug read failed: ${response.status}`);
    const page = await response.json() as DebugPage;
    if (page.chat_id !== chatId || !Array.isArray(page.messages)) throw new Error('Invalid Chat debug response');
    if (page.has_more && page.next_cursor <= cursor) throw new Error('Chat debug cursor did not advance');
    added.push(...page.messages);
    if (page.artifacts) artifacts = page.artifacts;
    if (page.turns) turns = page.turns;
    cursor = page.next_cursor;
    first = false;
    if (!page.has_more) break;
  } while (!signal.aborted);
  signal.throwIfAborted();
  return { chat_id: chatId, messages: [...(baseline?.messages ?? []), ...added], artifacts, turns, next_cursor: cursor };
}

/** No token polling or snapshot files. Refresh only at conversation boundaries. */
export function useChatDebugTranscript(chatId: string) {
  const client = useQueryClient();
  const boundary = useChatStreamStore(state => {
    const runtime = state.runtimes[chatId];
    // beginTurn is optimistic and precedes persistence. Wait for the actual
    // accepted server turn ID, then refresh again when projection completes.
    return runtime?.turnId ? `${runtime.turnId}:${runtime.projectionActive ? 'accepted' : 'finished'}` : null;
  });
  const previous = useRef({ chatId, boundary });
  const query = useQuery({
    queryKey: ['chat-debug-transcript', chatId],
    queryFn: ({ signal, queryKey }) => readDebugIncrement(chatId, client.getQueryData<DebugTranscript>(queryKey), signal),
    enabled: !!chatId,
    staleTime: 0,
    gcTime: 5 * 60 * 1000,
    refetchOnWindowFocus: true,
  });
  useEffect(() => {
    const changed = previous.current.chatId === chatId && previous.current.boundary !== boundary;
    previous.current = { chatId, boundary };
    if (!changed || boundary === null) return;
    // User acceptance and terminal events both follow durable message writes.
    const timer = window.setTimeout(() => {
      void client.invalidateQueries({ queryKey: ['chat-debug-transcript', chatId] });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [boundary, chatId, client]);
  return query;
}
