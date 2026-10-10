import { afterEach, describe, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({ connect: vi.fn(), reconcile: vi.fn(async () => {}), lease: vi.fn(async () => []) }));
vi.mock('@microsoft/fetch-event-source', () => ({ fetchEventSource: mocks.connect }));
vi.mock('../chat-reconcile', () => ({ reconcileChatWithServer: mocks.reconcile }));
vi.mock('../server-active-turn', () => ({ readServerActiveTurns: mocks.lease }));
import { useChatStreamStore } from '@/stores/chat-stream';
import { watchChatActivity } from '../chat-activity';

afterEach(() => { vi.clearAllMocks(); vi.useRealTimers(); useChatStreamStore.getState().reset(); });

describe('chat activity discovery', () => {
  it('checks worker leases only while running and cleans up the timer', async () => {
    vi.useFakeTimers();
    mocks.connect.mockReturnValue(new Promise(() => {}));
    const stop = watchChatActivity({ wfId: 's', chatId: 'c' });
    await vi.advanceTimersByTimeAsync(120_000);
    expect(mocks.lease).not.toHaveBeenCalled();
    useChatStreamStore.getState().beginTurn('c', 'turn');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(mocks.lease).toHaveBeenCalledTimes(1);
    useChatStreamStore.getState().setState('complete', 'c');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(mocks.lease).toHaveBeenCalledTimes(1);
    stop();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('does not lose a completion invalidation while the initial snapshot is pending', async () => {
    vi.useFakeTimers();
    mocks.connect.mockReturnValue(new Promise(() => {}));
    let finish!: () => void;
    mocks.reconcile.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve; }));
    const stop = watchChatActivity({ wfId: 'scope', chatId: 'chat' });
    const options = mocks.connect.mock.calls.at(-1)![1];
    options.onmessage({ event: 'changed' });
    await vi.advanceTimersByTimeAsync(100);
    options.onmessage({ event: 'changed' });
    expect(mocks.reconcile).toHaveBeenCalledTimes(1);
    finish();
    await vi.advanceTimersByTimeAsync(100);
    expect(mocks.reconcile).toHaveBeenCalledTimes(2);
    options.onmessage({ event: 'heartbeat' });
    expect(mocks.reconcile).toHaveBeenCalledTimes(2);
    stop();
    expect(options.signal.aborted).toBe(true);
  });

  it('opens independent transports and retries EOF, but stops on revoked access', async () => {
    vi.useFakeTimers();
    mocks.connect.mockReturnValue(new Promise(() => {}));
    const first = watchChatActivity({ wfId: 's', chatId: 'c' });
    const second = watchChatActivity({ wfId: 's', chatId: 'c' });
    const a = mocks.connect.mock.calls.at(-2)![1];
    const b = mocks.connect.mock.calls.at(-1)![1];
    first();
    expect(a.signal.aborted).toBe(true);
    expect(b.signal.aborted).toBe(false);
    expect(() => b.onclose()).toThrow('disconnected');
    await expect(b.onopen(new Response('', { status: 403 }))).rejects.toThrow();
    expect(b.signal.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
    second();
  });
});

it('recovers the final notification after read failure without a new event', async () => {
  vi.useFakeTimers();
  mocks.connect.mockReturnValue(new Promise(() => {}));
  mocks.reconcile.mockRejectedValueOnce(new Error('offline')).mockResolvedValue(undefined);
  const stop = watchChatActivity({ wfId: 'scope', chatId: 'chat' });
  const options = mocks.connect.mock.calls.at(-1)![1];
  options.onmessage({ event: 'changed' });
  await vi.advanceTimersByTimeAsync(100);
  expect(mocks.reconcile).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1000);
  expect(mocks.reconcile).toHaveBeenCalledTimes(2);
  await vi.advanceTimersByTimeAsync(60_000);
  expect(mocks.reconcile).toHaveBeenCalledTimes(2);
  stop();
  expect(vi.getTimerCount()).toBe(0);
});

it('cancels a pending failed-read retry when the chat closes', async () => {
  vi.useFakeTimers();
  mocks.connect.mockReturnValue(new Promise(() => {}));
  mocks.reconcile.mockRejectedValueOnce(new Error('offline'));
  const stop = watchChatActivity({ wfId: 'scope', chatId: 'chat' });
  mocks.connect.mock.calls.at(-1)![1].onmessage({ event: 'changed' });
  await vi.advanceTimersByTimeAsync(100);
  stop();
  await vi.advanceTimersByTimeAsync(60_000);
  expect(mocks.reconcile).toHaveBeenCalledTimes(1);
  expect(vi.getTimerCount()).toBe(0);
});
