import { QueryObserver } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { queryClient } from '@/app/query-client';

const mocks = vi.hoisted(() => ({ readServerActiveTurns: vi.fn() }));
vi.mock('../server-active-turn', () => ({ readServerActiveTurns: mocks.readServerActiveTurns }));
vi.mock('../active-turn', () => ({ readActiveTurnFor: () => null }));
vi.mock('../resume-turn', () => ({ resumeActiveTurn: vi.fn() }));

import { reconcileChatWithServer } from '../chat-reconcile';

const chat = { wfId: 'scope-1', chatId: 'chat-1' };
const historyKey = ['chat-history', chat.wfId, chat.chatId, null];
let unsubscribe: (() => void) | undefined;

function observeHistory(queryFn: () => Promise<string[]>) {
  queryClient.setQueryData(historyKey, ['old message']);
  const observer = new QueryObserver(queryClient, {
    queryKey: historyKey, queryFn, staleTime: Infinity, retry: false,
  });
  unsubscribe = observer.subscribe(() => undefined);
}

describe('chat reconciliation request budget', () => {
  beforeEach(() => {
    mocks.readServerActiveTurns.mockReset().mockResolvedValue([]);
  });
  afterEach(() => {
    unsubscribe?.();
    unsubscribe = undefined;
    queryClient.clear();
  });

  it('fetches an observed transcript once and installs the new content', async () => {
    const fetchHistory = vi.fn(async () => ['new message']);
    observeHistory(fetchHistory);

    await reconcileChatWithServer(chat);

    expect(fetchHistory).toHaveBeenCalledTimes(1);
    expect(queryClient.getQueryData(historyKey)).toEqual(['new message']);
  });

  it('shares overlapping focus/reconnect work but lets later events refresh again', async () => {
    let finishDiscovery!: (turns: []) => void;
    mocks.readServerActiveTurns.mockReturnValueOnce(new Promise((resolve) => { finishDiscovery = resolve; }));
    const fetchHistory = vi.fn(async () => ['new message']);
    observeHistory(fetchHistory);

    const focus = reconcileChatWithServer(chat);
    const reconnect = reconcileChatWithServer(chat);
    expect(mocks.readServerActiveTurns).toHaveBeenCalledTimes(1);
    finishDiscovery([]);
    await Promise.all([focus, reconnect]);
    expect(fetchHistory).toHaveBeenCalledTimes(1);

    await reconcileChatWithServer(chat);
    expect(fetchHistory).toHaveBeenCalledTimes(2);
    expect(mocks.readServerActiveTurns).toHaveBeenCalledTimes(2);
  });

  it('drains an older history read and then fetches the latest snapshot', async () => {
    let finishHistory!: (messages: string[]) => void;
    const fetchHistory = vi.fn().mockImplementationOnce(() => new Promise<string[]>((resolve) => { finishHistory = resolve; })).mockResolvedValue(['latest message']);
    observeHistory(fetchHistory);
    const existingRead = queryClient.refetchQueries({ queryKey: historyKey });
    const reconciliation = reconcileChatWithServer(chat);
    // Let discovery finish and reconciliation join the pending query.
    await vi.waitFor(() => expect(mocks.readServerActiveTurns).toHaveBeenCalledTimes(1));
    finishHistory(['older message']);
    await Promise.all([existingRead, reconciliation]);

    expect(fetchHistory).toHaveBeenCalledTimes(2);
    expect(queryClient.getQueryData(historyKey)).toEqual(['latest message']);
  });
});

it('surfaces a failed observed history read and preserves its existing data', async () => {
  mocks.readServerActiveTurns.mockResolvedValue([]);
  observeHistory(async () => { throw new Error('history offline'); });
  try {
    await expect(reconcileChatWithServer(chat, { throwOnError: true })).rejects.toThrow('history offline');
    expect(queryClient.getQueryData(historyKey)).toEqual(['old message']);
  } finally {
    unsubscribe?.();
    queryClient.clear();
  }
});

it('reads again when an activity event arrives during an older focus reconciliation', async () => {
  let finish!: (turns: []) => void;
  mocks.readServerActiveTurns.mockReturnValueOnce(new Promise<[]>((resolve) => { finish = resolve; })).mockResolvedValue([]);
  const focus = reconcileChatWithServer(chat);
  const activity = reconcileChatWithServer(chat, { throwOnError: true });
  const initialCalls = mocks.readServerActiveTurns.mock.calls.length;
  finish([]);
  await Promise.all([focus, activity]);
  expect(mocks.readServerActiveTurns.mock.calls.length).toBe(initialCalls + 1);
});
