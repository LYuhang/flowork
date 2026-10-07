import { afterEach, expect, it, vi } from 'vitest';
import { fetchTurnHandoffHistory } from '../queries/chats';
import { mergeHistoryWindow } from '@/pages/chat/history-window';

const rows = Array.from({ length: 550 }, (_, i) => ({ id: String(i), role: 'assistant', content: `message ${i}`, history_position: i }));
afterEach(() => vi.unstubAllGlobals());

it('keeps a long completed turn and all previously loaded history without another load-more action', async () => {
  const calls: { offset: number; limit: number }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const p = new URL(url, 'https://example.test').searchParams;
    const limit = Number(p.get('limit'));
    const offset = p.get('tail') ? rows.length - limit : Number(p.get('offset'));
    calls.push({ offset, limit });
    return Response.json({ items: rows.slice(offset, offset + limit), offset, limit, total: rows.length });
  }));
  const boundary = { items: rows.slice(0, 50), offset: 0, limit: 50, total: 50 };
  const page = await fetchTurnHandoffHistory('scope', 'chat', 'turn', boundary);
  const visible = mergeHistoryWindow(mergeHistoryWindow(undefined, boundary), page);
  expect(visible.items.map(r => r.id)).toEqual(rows.map(r => r.id));
  expect(visible.offset).toBe(0);
  expect(calls).toEqual([{ offset: 520, limit: 30 }, { offset: 50, limit: 200 },
    { offset: 250, limit: 200 }, { offset: 450, limit: 70 }]);
});

it('does not refetch older pages when the normal tail already covers the completed turn', async () => {
  const fetcher = vi.fn(async () => Response.json({ items: rows.slice(20, 50), offset: 20, limit: 30, total: 50 }));
  vi.stubGlobal('fetch', fetcher);
  const page = await fetchTurnHandoffHistory('scope', 'chat', 'turn', { items: [], offset: 0, limit: 30, total: 40 });
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(page.offset).toBe(20);
});

it('does not hand off a partial transcript when filling the visible turn fails', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ items: rows.slice(520), offset: 520, limit: 30, total: 550 }))
    .mockResolvedValueOnce(new Response('', { status: 503 }));
  vi.stubGlobal('fetch', fetcher);
  await expect(fetchTurnHandoffHistory('scope', 'chat', 'turn', { items: [], offset: 0, limit: 30, total: 50 })).rejects.toThrow('503');
});
