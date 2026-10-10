import { getApiBase } from '@/lib/base-path';
import { useAuthStore } from '@/stores/auth';

export interface ChatInputReceipt {
  input_id: string;
  chat_id: string;
  run_id: string;
  text: string;
  status: 'submitting' | 'accepted' | 'rejected' | 'unknown';
  error_code: string | null;
}
export interface ChatInputControls {
  chat_id: string;
  run_id: string;
  supported_controls: string[];
  available_controls: string[];
  unavailable_reason: string | null;
  latest_input: ChatInputReceipt | null;
}
export const inputControlsKey = (chatId: string, runId: string | null) => ['chat-input-controls', chatId, runId ?? 'latest'] as const;
const path = (chatId: string) => `${getApiBase()}/api/v1/chats/${encodeURIComponent(chatId)}`;
async function responseJson<T>(response: Response): Promise<T> {
  if (response.status === 401) useAuthStore.getState().handle401();
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json() as Promise<T>;
}
export const fetchInputControls = (chatId: string, runId: string | null, signal?: AbortSignal) =>
  fetch(`${path(chatId)}${runId ? `/runs/${encodeURIComponent(runId)}` : ''}/input-controls`, { signal })
    .then(responseJson<ChatInputControls>);
export const submitChatInput = (chatId: string, body: { input_id: string; run_id: string; text: string }) =>
  fetch(`${path(chatId)}/inputs`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }).then(responseJson<ChatInputReceipt>);
