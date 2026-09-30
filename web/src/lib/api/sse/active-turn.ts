// Client-side replay cursor for server-confirmed active turns.
//
// This file deliberately does not decide whether a turn exists. Active turns,
// pending HITL, and terminal state are discovered from the backend control
// plane; localStorage only preserves the last applied event id for a smoother
// same-browser resume.
import type { components } from '@/lib/api/schema';

const KEY = 'vibecanvas.activeTurn';
const MANY_KEY = 'vibecanvas.activeTurns';
type Attachment = NonNullable<components['schemas']['MessagePostBody']['attachments']>[number];

export interface ActiveTurn {
  wfId: string;
  chatId: string;
  turnId: string;
  status?: 'running' | 'waiting_approval' | 'cancel_requested';
  lastEventId?: number;
  inputMessage?: {
    id?: string | null;
    role: 'user';
    content: string;
    attachments?: Attachment[];
  };
  pendingHitl?: Array<{
    hitlRequestId: string;
    hitlType: string;
    status: string;
    title?: string;
    promptText?: string;
    uiPayload?: Record<string, unknown>;
    uiProjectionEvent?: Record<string, unknown>;
  }>;
}

export function rememberActiveTurn(t: ActiveTurn): void {
  if (!t.chatId || !t.turnId) return;
  try {
    const turns = readActiveTurns();
    const existing = turns.find(
      (item) => item.wfId === t.wfId && item.chatId === t.chatId,
    );
    const nextTurn: ActiveTurn = {
      wfId: t.wfId,
      chatId: t.chatId,
      turnId: t.turnId,
      status: t.status ?? existing?.status,
      lastEventId: t.lastEventId ?? existing?.lastEventId ?? 0,
      inputMessage: t.inputMessage ?? existing?.inputMessage,
      pendingHitl: t.pendingHitl ?? existing?.pendingHitl ?? [],
    };
    const next = turns.filter(
      (item) => !(item.wfId === t.wfId && item.chatId === t.chatId),
    );
    next.push(nextTurn);
    localStorage.setItem(MANY_KEY, JSON.stringify(next));
    localStorage.setItem(KEY, JSON.stringify(nextTurn));
  } catch {
    /* storage unavailable — resume just won't be offered */
  }
}

export function updateActiveTurnCursor(
  t: Pick<ActiveTurn, 'wfId' | 'chatId' | 'turnId'>,
  eventId: number,
): void {
  if (!Number.isFinite(eventId) || eventId <= 0) return;
  try {
    const turns = readActiveTurns();
    let changed = false;
    const next = turns.map((item) => {
      if (item.wfId !== t.wfId || item.chatId !== t.chatId || item.turnId !== t.turnId) {
        return item;
      }
      if ((item.lastEventId ?? 0) >= eventId) return item;
      changed = true;
      return { ...item, lastEventId: eventId };
    });
    if (!changed) return;
    localStorage.setItem(MANY_KEY, JSON.stringify(next));
    localStorage.setItem(KEY, JSON.stringify(next[next.length - 1]));
  } catch {
    /* no-op */
  }
}

export function readActiveTurns(): ActiveTurn[] {
  try {
    const many = localStorage.getItem(MANY_KEY);
    if (many) {
      const parsed = JSON.parse(many);
      if (Array.isArray(parsed)) {
        return parsed
          .map((p) => p as Partial<ActiveTurn>)
          .filter((p) => p && p.wfId && p.chatId && p.turnId)
          .map((p) => ({
            wfId: p.wfId as string,
            chatId: p.chatId as string,
            turnId: p.turnId as string,
            status: p.status,
            lastEventId: typeof p.lastEventId === 'number' ? p.lastEventId : 0,
            inputMessage: p.inputMessage,
            pendingHitl: Array.isArray(p.pendingHitl) ? p.pendingHitl : [],
          }));
      }
    }
    const one = localStorage.getItem(KEY);
    if (!one) return [];
    const p = JSON.parse(one) as Partial<ActiveTurn>;
    return p && p.wfId && p.chatId && p.turnId
      ? [{
          wfId: p.wfId,
          chatId: p.chatId,
          turnId: p.turnId,
          status: p.status,
          lastEventId: typeof p.lastEventId === 'number' ? p.lastEventId : 0,
          inputMessage: p.inputMessage,
          pendingHitl: Array.isArray(p.pendingHitl) ? p.pendingHitl : [],
        }]
      : [];
  } catch {
    return [];
  }
}

export function readActiveTurn(): ActiveTurn | null {
  return readActiveTurns().at(-1) ?? null;
}

export function readActiveTurnFor(wfId: string, chatId: string): ActiveTurn | null {
  return readActiveTurns().find((t) => t.wfId === wfId && t.chatId === chatId) ?? null;
}

export function clearActiveTurn(t?: Pick<ActiveTurn, 'wfId' | 'chatId'>): void {
  try {
    if (!t) {
      localStorage.removeItem(KEY);
      localStorage.removeItem(MANY_KEY);
      return;
    }
    const turns = readActiveTurns().filter(
      (existing) => !(existing.wfId === t.wfId && existing.chatId === t.chatId),
    );
    if (turns.length) {
      localStorage.setItem(MANY_KEY, JSON.stringify(turns));
      localStorage.setItem(KEY, JSON.stringify(turns[turns.length - 1]));
    } else {
      localStorage.removeItem(KEY);
      localStorage.removeItem(MANY_KEY);
    }
  } catch {
    /* no-op */
  }
}
