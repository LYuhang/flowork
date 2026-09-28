import { useChatStreamStore } from '@/stores/chat-stream';
import { useUIStore } from '@/stores/ui';
import { chatAccountNamespace, writeRecentChatSelection, writeRecentDraftSelection, type ChatAccountIdentity } from './state-key';

const PREFIX = 'vibecanvas:project-draft:v1:';
const fallbackIds = new Map<string, string>();

function draftKey(account: ChatAccountIdentity | null | undefined, projectId: string) {
  return `${PREFIX}${chatAccountNamespace(account)}:${projectId}`;
}

/** One local composer per Project. Opening it never creates server history. */
export function openProjectChatDraft({ account, projectId, scopeId, startedChatIds = [] }: {
  account: ChatAccountIdentity | null | undefined;
  projectId: string;
  scopeId: string;
  startedChatIds?: string[];
}): string {
  const key = draftKey(account, projectId);
  let chatId = fallbackIds.get(key) ?? null;
  try { chatId = window.localStorage.getItem(key) ?? chatId; } catch { /* In-memory draft still works. */ }
  const ui = useUIStore.getState();
  if (!chatId || !/^[A-Za-z0-9_-]{1,128}$/.test(chatId)
    || startedChatIds.includes(chatId)
    || ui.optimisticChatSessions.some((chat) => chat.chat_id === chatId)
    || useChatStreamStore.getState().runtimes[chatId]?.projectionActive) {
    chatId = crypto.randomUUID();
  }
  fallbackIds.set(key, chatId);
  try { window.localStorage.setItem(key, chatId); } catch { /* In-memory draft still works. */ }
  writeRecentDraftSelection(account, { chatId, scopeId, projectId });
  useUIStore.setState({
    activeProjectId: projectId,
    activeChatIds: { ...ui.activeChatIds, chat: chatId },
    chatEntryIntent: 'select',
  });
  return chatId;
}

/** Only durable acceptance promotes a draft; a rejected send retains it. */
export function acceptProjectChatDraft(account: ChatAccountIdentity | null | undefined, projectId: string, scopeId: string, chatId: string) {
  const key = draftKey(account, projectId);
  if (fallbackIds.get(key) === chatId) fallbackIds.delete(key);
  try {
    if (window.localStorage.getItem(key) === chatId) window.localStorage.removeItem(key);
  } catch { /* No server state depends on local storage. */ }
  if (useUIStore.getState().activeChatIds.chat === chatId) {
    try { writeRecentChatSelection(account, 'chat', chatId, scopeId); } catch { /* Acceptance must not depend on Web Storage. */ }
  }
}

export function clearProjectDraftMemory() {
  fallbackIds.clear();
}
