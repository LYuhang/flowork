import { getApiBase, getBasePath } from '@/lib/base-path';
import { useAuthStore } from '@/stores/auth';

export type MessageRating = 'up' | 'down' | null;
export interface ChatShare {
  id: string;
  message_id: string | null;
  path: string;
  url: string | null;
  created_at: string;
  expires_at: string | null;
  message_count: number;
}
export interface SharedTranscript {
  title: string;
  kind: 'message' | 'conversation';
  created_at: string;
  messages: Array<{ id: string; role: 'user' | 'assistant'; content: string }>;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${getApiBase()}/api/v1${path}`, init);
  if (response.status === 401) useAuthStore.getState().handle401();
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}

export const feedbackKey = (chatId: string) => ['chat-feedback', chatId] as const;
export const sharesKey = (chatId: string) => ['chat-shares', chatId] as const;
const chatPath = (chatId: string) => `/chats/${encodeURIComponent(chatId)}`;

export const fetchFeedback = (chatId: string) =>
  request<{ ratings: Record<string, MessageRating> }>(`${chatPath(chatId)}/feedback`);

export const putFeedback = (chatId: string, messageId: string, rating: MessageRating) =>
  request(`${chatPath(chatId)}/messages/${encodeURIComponent(messageId)}/feedback`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rating }),
  });

export const fetchChatShares = (chatId: string) =>
  request<{ items: ChatShare[] }>(`${chatPath(chatId)}/shares`);

export const previewChatShare = (chatId: string, messageId?: string) =>
  request<{ messages: SharedTranscript['messages']; existing: boolean }>(`${chatPath(chatId)}/shares/preview`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message_id: messageId ?? null }),
  });

export const createChatShare = (chatId: string, messageId?: string) =>
  request<ChatShare>(`${chatPath(chatId)}/shares`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message_id: messageId ?? null }),
  });

export const revokeChatShare = (chatId: string, shareId: string) =>
  request<void>(`${chatPath(chatId)}/shares/${encodeURIComponent(shareId)}`, { method: 'DELETE' });

export const shareUrl = (share: ChatShare) => share.url
  ?? `${window.location.origin}${getBasePath()}${share.path}`;

export async function fetchPublicShare(token: string): Promise<SharedTranscript> {
  // This route deliberately works without the app's authentication bootstrap.
  const response = await fetch(`${getApiBase()}/api/v1/public/chat-shares/${encodeURIComponent(token)}`, {
    credentials: 'omit', cache: 'no-store',
  });
  if (!response.ok) throw new Error(response.status === 404 ? 'unavailable' : 'network');
  return response.json() as Promise<SharedTranscript>;
}

export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  // Local HTTP development and older embedded browsers lack Clipboard API.
  const input = document.createElement('textarea');
  input.value = text;
  input.style.position = 'fixed';
  input.style.opacity = '0';
  document.body.appendChild(input);
  input.select();
  const copied = document.execCommand('copy');
  input.remove();
  if (!copied) throw new Error('clipboard_unavailable');
}
