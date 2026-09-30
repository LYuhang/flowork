import { useAuthStore } from '@/stores/auth';
import { getApiBase } from '@/lib/base-path';
import type { components } from '@/lib/api/schema';
import {
  clearActiveTurn,
  readActiveTurns,
  rememberActiveTurn,
  type ActiveTurn,
} from './active-turn';

type Attachment = NonNullable<components['schemas']['MessagePostBody']['attachments']>[number];

interface ActiveRunResponse {
  run_id: string;
  chat_id: string;
  status: 'running' | 'waiting_approval' | 'cancel_requested';
  last_event_id?: number;
  input_message?: {
    id?: string | null;
    role: 'user';
    content: string;
    attachments?: Attachment[];
  } | null;
  pending_hitl?: Array<{
    hitl_request_id: string;
    hitl_type: string;
    status: string;
    title?: string;
    prompt_text?: string;
    ui_payload_json?: Record<string, unknown>;
    ui_projection_event_json?: Record<string, unknown>;
  }>;
}

/**
 * Discover active turns from the server-side control plane. Core run/HITL state
 * is backend-authoritative; localStorage only stores a replay cursor and never
 * decides whether a turn exists.
 */
export async function readServerActiveTurns(wfId: string): Promise<ActiveTurn[] | null> {
  const token = useAuthStore.getState().token;
  const base = getApiBase();
  try {
    const response = await fetch(
      `${base}/api/v1/chat-scopes/${encodeURIComponent(wfId)}/active-runs`,
      {
        headers: {
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          Accept: 'application/json',
        },
      },
    );
    if (response.status === 401) {
      useAuthStore.getState().handle401();
      return null;
    }
    if (!response.ok) return null;
    const rows = await response.json() as ActiveRunResponse[];
    const local = readActiveTurns().filter((turn) => turn.wfId === wfId);
    const serverKeys = new Set(rows.map((row) => `${row.chat_id}:${row.run_id}`));
    for (const stale of local) {
      if (!serverKeys.has(`${stale.chatId}:${stale.turnId}`)) {
        clearActiveTurn({ wfId, chatId: stale.chatId });
      }
    }
    return rows.map((row) => {
      const cached = local.find(
        (turn) => turn.chatId === row.chat_id && turn.turnId === row.run_id,
      );
      const turn: ActiveTurn = {
        wfId,
        chatId: row.chat_id,
        turnId: row.run_id,
        status: row.status,
        // This remains a client projection cursor. A fresh frontend has no
        // projection, and resumeActiveTurn deliberately restarts from zero.
        lastEventId: cached?.lastEventId ?? 0,
        inputMessage: row.input_message ?? undefined,
        pendingHitl: Array.isArray(row.pending_hitl)
          ? row.pending_hitl.map((item) => ({
              hitlRequestId: item.hitl_request_id,
              hitlType: item.hitl_type,
              status: item.status,
              title: item.title,
              promptText: item.prompt_text,
              uiPayload: item.ui_payload_json,
              uiProjectionEvent: item.ui_projection_event_json,
            }))
          : [],
      };
      rememberActiveTurn(turn);
      return turn;
    });
  } catch {
    return null;
  }
}
