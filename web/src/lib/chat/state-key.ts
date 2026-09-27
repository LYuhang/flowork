export interface ChatAccountIdentity {
  tenant_id?: string | null;
  user_id?: string | null;
}

const RECENT_CHAT_SELECTION_PREFIX = 'vibecanvas:recent-chat:v1:';
const OPAQUE_ID_PATTERN = /^[A-Za-z0-9_-]+$/;

export interface RecentChatLocation {
  chatId: string;
  scopeId: string | null;
}

export function chatAccountNamespace(identity: ChatAccountIdentity | null | undefined): string {
  if (!identity) return 'anonymous';
  return `${identity.tenant_id || 'tenant-pending'}:${identity.user_id || 'user-pending'}`;
}

function recentChatSelectionKey(
  identity: ChatAccountIdentity | null | undefined,
  surface: 'chat' | 'browser',
): string {
  return `${RECENT_CHAT_SELECTION_PREFIX}${chatAccountNamespace(identity)}:${surface}`;
}

/**
 * Remember only the opaque id of the last conversation selected in this tab.
 *
 * Keeping the transcript itself out of Web Storage avoids creating another
 * plaintext message store. The id is enough to start the authorized history
 * request as soon as bootstrap completes, in parallel with the full session
 * inventory and active-run reconciliation.
 */
export function readRecentChatLocation(
  identity: ChatAccountIdentity | null | undefined,
  surface: 'chat' | 'browser',
): RecentChatLocation | null {
  if (typeof window === 'undefined') return null;
  const value = window.sessionStorage.getItem(recentChatSelectionKey(identity, surface));
  if (!value) return null;
  // Read the original id-only format during a rolling frontend update.
  if (value.length <= 128 && OPAQUE_ID_PATTERN.test(value)) {
    return { chatId: value, scopeId: null };
  }
  try {
    const parsed = JSON.parse(value) as { chat_id?: unknown; scope_id?: unknown };
    const chatId = typeof parsed.chat_id === 'string' ? parsed.chat_id : '';
    const scopeId = typeof parsed.scope_id === 'string' ? parsed.scope_id : '';
    if (
      !chatId
      || chatId.length > 128
      || !OPAQUE_ID_PATTERN.test(chatId)
      || !scopeId
      || scopeId.length > 160
      || !OPAQUE_ID_PATTERN.test(scopeId)
    ) return null;
    return { chatId, scopeId };
  } catch {
    return null;
  }
}

export function readRecentChatSelection(
  identity: ChatAccountIdentity | null | undefined,
  surface: 'chat' | 'browser',
): string | null {
  return readRecentChatLocation(identity, surface)?.chatId ?? null;
}

export function writeRecentChatSelection(
  identity: ChatAccountIdentity | null | undefined,
  surface: 'chat' | 'browser',
  chatId: string | null,
  scopeId?: string | null,
): void {
  if (typeof window === 'undefined') return;
  const key = recentChatSelectionKey(identity, surface);
  if (chatId && scopeId) {
    window.sessionStorage.setItem(key, JSON.stringify({ chat_id: chatId, scope_id: scopeId }));
  } else if (chatId) window.sessionStorage.setItem(key, chatId);
  else window.sessionStorage.removeItem(key);
}

/** Clear account-scoped navigation hints on logout or organization switch. */
export function clearRecentChatSelections(): void {
  if (typeof window === 'undefined') return;
  for (let index = window.sessionStorage.length - 1; index >= 0; index -= 1) {
    const key = window.sessionStorage.key(index);
    if (key?.startsWith(RECENT_CHAT_SELECTION_PREFIX)) {
      window.sessionStorage.removeItem(key);
    }
  }
}

export function chatClientStateKey({
  account,
  scopeId,
  surface,
  chatId,
  suffix,
}: {
  account: ChatAccountIdentity | null | undefined;
  scopeId: string;
  surface: 'chat' | 'browser';
  chatId: string;
  suffix?: string;
}): string {
  return [
    chatAccountNamespace(account),
    scopeId || 'scope-pending',
    surface,
    chatId || 'draft',
    suffix,
  ].filter(Boolean).join(':');
}
