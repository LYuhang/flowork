import { getApiBase } from '@/lib/base-path';
import { useAuthStore } from '@/stores/auth';
import type { ChatAttachment } from '@/components/agent-sidebar/chat-attachments';
import type { DraftOperation, ServerDraft } from '@/lib/chat/context-draft';

export const CONTEXT_DRAFT_EVENT = 'flowork:context-draft';
export function notifyContextDraft(chatId: string, local = true) {
  if (local) window.dispatchEvent(new CustomEvent(CONTEXT_DRAFT_EVENT, {detail:chatId}));
  if (typeof BroadcastChannel !== 'undefined') {
    const channel = new BroadcastChannel(CONTEXT_DRAFT_EVENT);
    channel.postMessage({chatId}); channel.close();
  }
}
async function request(chatId: string, operation?: DraftOperation, retried = false): Promise<ServerDraft> {
  const token = useAuthStore.getState().token;
  const response = await fetch(`${getApiBase()}/api/v1/chats/${encodeURIComponent(chatId)}/draft${operation ? '/operations' : ''}`, {
    method:operation ? 'POST' : 'GET',
    headers:{...(token ? {Authorization:`Bearer ${token}`} : {}),...(operation ? {'Content-Type':'application/json'} : {})},
    ...(operation ? {body:JSON.stringify(operation)} : {}),
  });
  if (response.status === 401) useAuthStore.getState().handle401();
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    // The authorization service can be briefly unavailable during startup.
    // Retry reads once; mutations retain the caller's explicit receipt/retry flow.
    if (!operation && !retried && response.status === 503 && body.detail?.code === 'authorization_unavailable') {
      await new Promise(resolve => setTimeout(resolve, 1000));
      return request(chatId, undefined, true);
    }
    throw new Error(typeof body.detail === 'string' ? body.detail : `draft_request_failed_${response.status}`);
  }
  return response.json();
}
export const fetchContextDraft = (chatId: string) => request(chatId);
export async function mutateContextDraft(chatId: string, operation: DraftOperation, notifyLocal = true) {
  const result = await request(chatId, operation);
  notifyContextDraft(chatId, notifyLocal);
  return result;
}
export async function addContextToChat(chatId: string, attachments: ChatAttachment[], operationId: string = crypto.randomUUID()) {
  return mutateContextDraft(chatId, {kind:'append',operation_id:operationId,attachments});
}
