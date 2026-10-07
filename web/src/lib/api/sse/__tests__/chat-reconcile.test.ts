import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  invalidateQueries: vi.fn(async () => undefined),
  refetchQueries: vi.fn(async () => undefined),
  readServerActiveTurns: vi.fn(),
  readActiveTurnFor: vi.fn(),
  resumeActiveTurn: vi.fn(async () => true),
}));

vi.mock('@/app/query-client', () => ({
  queryClient: {
    invalidateQueries: mocks.invalidateQueries,
    refetchQueries: mocks.refetchQueries,
  },
}));
vi.mock('../server-active-turn', () => ({
  readServerActiveTurns: mocks.readServerActiveTurns,
}));
vi.mock('../active-turn', () => ({
  readActiveTurnFor: mocks.readActiveTurnFor,
}));
vi.mock('../resume-turn', () => ({
  resumeActiveTurn: mocks.resumeActiveTurn,
}));

import { reconcileChatWithServer } from '../chat-reconcile';

describe('reconcileChatWithServer terminal recovery', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.readServerActiveTurns.mockResolvedValue([]);
    mocks.readActiveTurnFor.mockReturnValue(null);
  });

  it('replays a locally streaming Turn missing from the authoritative active set', async () => {
    const localTurn = {
      wfId: 'scope-1',
      chatId: 'chat-1',
      turnId: 'turn-1',
      lastEventId: 41,
    };
    mocks.readActiveTurnFor.mockReturnValue(localTurn);

    await reconcileChatWithServer({ wfId: 'scope-1', chatId: 'chat-1' });

    expect(mocks.resumeActiveTurn).toHaveBeenCalledTimes(1);
    expect(mocks.resumeActiveTurn).toHaveBeenCalledWith(localTurn);
  });

  it('does not start a second replay for a Turn still reported active', async () => {
    const localTurn = {
      wfId: 'scope-1',
      chatId: 'chat-1',
      turnId: 'turn-1',
      lastEventId: 9,
    };
    mocks.readActiveTurnFor.mockReturnValue(localTurn);
    mocks.readServerActiveTurns.mockResolvedValue([localTurn]);

    await reconcileChatWithServer({ wfId: 'scope-1', chatId: 'chat-1' });

    expect(mocks.resumeActiveTurn).toHaveBeenCalledTimes(1);
    expect(mocks.resumeActiveTurn).toHaveBeenCalledWith(localTurn);
  });

  it('keeps the terminal replay cursor before discovery clears storage', async () => {
    const turn = { wfId: 'scope-1', chatId: 'chat-1', turnId: 'ended', lastEventId: 8 };
    mocks.readActiveTurnFor.mockReturnValue(turn);
    mocks.readServerActiveTurns.mockImplementationOnce(async () => {
      mocks.readActiveTurnFor.mockReturnValue(null);
      return [];
    });
    await reconcileChatWithServer({ wfId: 'scope-1', chatId: 'chat-1' });
    expect(mocks.resumeActiveTurn).toHaveBeenCalledWith(turn);
  });

  it('only opens a replay for the selected conversation', async () => {
    const selected = { wfId: 'scope-1', chatId: 'chat-1', turnId: 'selected' };
    mocks.readServerActiveTurns.mockResolvedValue([
      { wfId: 'scope-1', chatId: 'other-chat', turnId: 'other' }, selected,
    ]);
    await reconcileChatWithServer({ wfId: 'scope-1', chatId: 'chat-1' });
    expect(mocks.resumeActiveTurn).toHaveBeenCalledTimes(1);
    expect(mocks.resumeActiveTurn).toHaveBeenCalledWith(selected);
  });

  it('does not infer completion when active-run discovery fails', async () => {
    mocks.readActiveTurnFor.mockReturnValue({
      wfId: 'scope-1',
      chatId: 'chat-1',
      turnId: 'turn-1',
      lastEventId: 9,
    });
    mocks.readServerActiveTurns.mockResolvedValue(null);

    await reconcileChatWithServer({ wfId: 'scope-1', chatId: 'chat-1' });

    expect(mocks.resumeActiveTurn).not.toHaveBeenCalled();
  });
});
