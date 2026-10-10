import { afterEach, expect, it, vi } from 'vitest';
import { createChatBranch } from './chat-branches';

afterEach(() => vi.unstubAllGlobals());
it.each([
  [409, '{"detail":"branch_turn_not_finished"}', 'branch_turn_not_finished'],
  [503, '{"detail":"authorization_unavailable"}', 'authorization_unavailable'],
  [502, '<html>Bad Gateway</html>', 'Bad Gateway'],
])('preserves HTTP %s diagnostics without resubmitting', async (status, body, detail) => {
  const fetch = vi.fn().mockResolvedValue(new Response(body, { status, statusText: detail }));
  vi.stubGlobal('fetch', fetch);
  await expect(createChatBranch('source', { chat_id: 'target', turn_id: 'turn', name: 'Branch' }))
    .rejects.toThrow(`Branch request failed (${status}): ${detail}`);
  expect(fetch).toHaveBeenCalledOnce();
});
