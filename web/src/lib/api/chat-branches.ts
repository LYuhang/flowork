import { getApiBase } from '@/lib/base-path';
import type { components } from './schema';

export type ChatBranch = components['schemas']['BranchOut'];
const branchPath = (chatId: string) => `${getApiBase()}/api/v1/chats/${encodeURIComponent(chatId)}/branches`;

async function readBranch(response: Response): Promise<ChatBranch> {
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const detail = body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'string'
      ? body.detail : response.statusText;
    throw new Error(`Branch request failed (${response.status})${detail ? `: ${detail}` : ''}`);
  }
  return response.json() as Promise<ChatBranch>;
}

export const previewChatBranch = (chatId: string, turnId: string) =>
  fetch(`${branchPath(chatId)}/preview?${new URLSearchParams({ turn_id: turnId })}`).then(readBranch);

export const createChatBranch = (sourceId: string, body: components['schemas']['BranchBody']) =>
  fetch(branchPath(sourceId), {
    method: 'POST', signal: AbortSignal.timeout(30_000), headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }).then(readBranch);
