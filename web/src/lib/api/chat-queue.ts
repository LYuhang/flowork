import { getApiBase } from '@/lib/base-path';
import { useAuthStore } from '@/stores/auth';
import type { components } from './schema';

export type QueuedChatInput = components['schemas']['QueuedInputOut'];
type QueueInputBody = components['schemas']['QueueInputBody'];

export const queuedInputKey = (chatId: string) => ['chat-queued-input', chatId] as const;
const path = (chatId: string) => `${getApiBase()}/api/v1/chats/${encodeURIComponent(chatId)}/queued-input`;

async function readResponse<T>(response: Response): Promise<T> {
  if (response.status === 401) useAuthStore.getState().handle401();
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = typeof body?.detail === 'string' ? body.detail : response.statusText;
    throw new Error(`HTTP ${response.status}: ${detail}`);
  }
  return response.json() as Promise<T>;
}

export const fetchQueuedInput = (chatId: string, signal?: AbortSignal) =>
  fetch(path(chatId), { signal }).then(readResponse<{ input: QueuedChatInput | null }>);

// Mutations are issued once. An uncertain response is resolved by reading the
// durable receipt, never by repeating a submission or sending a normal message.
export const queueChatInput = (chatId: string, body: QueueInputBody) => fetch(path(chatId), {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
}).then(readResponse<QueuedChatInput>);

export const cancelQueuedInput = (chatId: string, inputId: string) =>
  fetch(`${path(chatId)}/${encodeURIComponent(inputId)}`, { method: 'DELETE' })
    .then(readResponse<QueuedChatInput>);
