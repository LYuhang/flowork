import { getBasePath } from '@/lib/base-path';
import { createContext, useContext } from 'react';

/** The conversation that opened this preview; never inferred from resource ownership. */
export interface PreviewOrigin {
  chatId: string;
  messageId?: string;
  artifactId?: string;
}
export const PreviewOriginContext = createContext<PreviewOrigin | null>(null);
export const usePreviewOrigin = () => useContext(PreviewOriginContext);
export function previewOriginFromSearch(search: URLSearchParams): PreviewOrigin | null {
  const chatId = search.get('originChatId')?.trim();
  if (!chatId || chatId.length > 512) return null;
  const messageId = search.get('originMessageId')?.trim();
  const artifactId = search.get('originArtifactId')?.trim();
  return { chatId, ...(messageId && messageId.length <= 512 ? { messageId } : {}), ...(artifactId && artifactId.length <= 512 ? { artifactId } : {}) };
}
export function appendPreviewOrigin(query: URLSearchParams, origin?: PreviewOrigin | null) {
  if (!origin) return;
  query.set('originChatId', origin.chatId);
  if (origin.messageId) query.set('originMessageId', origin.messageId);
  if (origin.artifactId) query.set('originArtifactId', origin.artifactId);
}

export function originChatHref(chatId: string): string {
  return `${getBasePath()}/chat/open/${encodeURIComponent(chatId)}`;
}
