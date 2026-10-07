import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { readServerActiveTurns } from '../server-active-turn';
import { readActiveTurns, rememberActiveTurn } from '../active-turn';
import { useChatStreamStore } from '@/stores/chat-stream';

beforeEach(() => {
  localStorage.clear();
  useChatStreamStore.getState().reset();
});
afterEach(() => vi.unstubAllGlobals());

it.each(['complete', 'cancelled'] as const)('ignores discovery that arrives after %s while retaining another active chat', async (state) => {
  let resolve!: (response: Response) => void;
  vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((r) => { resolve = r; })));
  const store = useChatStreamStore.getState();
  store.beginTurn('chat', 'turn');
  rememberActiveTurn({ wfId: 'scope', chatId: 'chat', turnId: 'turn' });
  const discovery = readServerActiveTurns('scope');
  store.setState(state, 'chat');
  store.finishProjection('chat', 'turn');
  resolve(Response.json([
    { chat_id: 'chat', run_id: 'turn', status: 'running' },
    { chat_id: 'other-chat', run_id: 'other-turn', status: 'running' },
  ]));
  expect(await discovery).toMatchObject([{ chatId: 'other-chat', turnId: 'other-turn' }]);
  expect(readActiveTurns().map(t => t.chatId)).toEqual(['other-chat']);
  expect(useChatStreamStore.getState().runtimes.chat.state).toBe(state);
});

it('discovers a genuinely new turn after cancellation', async () => {
  const store = useChatStreamStore.getState();
  store.beginTurn('chat', 'old');
  store.setState('cancelled', 'chat');
  vi.stubGlobal('fetch', vi.fn(async () => Response.json([
    { chat_id: 'chat', run_id: 'new', status: 'running' },
  ])));
  expect(await readServerActiveTurns('scope')).toMatchObject([{ chatId: 'chat', turnId: 'new' }]);
});
