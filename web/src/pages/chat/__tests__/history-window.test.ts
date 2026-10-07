import { describe, expect, it } from 'vitest';
import { mergeHistoryWindow, type ChatHistoryWindow } from '../history-window';

const chunk = (id: string, content = id) => ({
  id,
  role: 'assistant' as const,
  content,
});

describe('mergeHistoryWindow', () => {
  it('prepends an earlier page ahead of the existing tail', () => {
    const tail: ChatHistoryWindow = {
      items: [chunk('new-1'), chunk('new-2')],
      total: 4,
      limit: 2,
      offset: 2,
    };

    const merged = mergeHistoryWindow(tail, {
      items: [chunk('old-1'), chunk('old-2')],
      total: 4,
      limit: 2,
      offset: 0,
    });

    expect(merged.items.map((item) => item.id)).toEqual([
      'old-1',
      'old-2',
      'new-1',
      'new-2',
    ]);
    expect(merged.offset).toBe(0);
  });

  it('lets a refreshed tail replace the previous copy of a message', () => {
    const previous: ChatHistoryWindow = {
      items: [chunk('tool-1', 'running')],
      total: 1,
      limit: 1,
      offset: 0,
    };

    const merged = mergeHistoryWindow(previous, {
      items: [chunk('tool-1', 'done')],
      total: 1,
      limit: 1,
      offset: 0,
    });

    expect(merged.items).toHaveLength(1);
    expect(merged.items[0]?.content).toBe('done');
  });
});

it('does not treat an empty pre-Turn checkpoint as loaded older history', () => {
  const merged = mergeHistoryWindow({ items: [], total: 0, limit: 30, offset: 0 }, {
    items: Array.from({ length: 30 }, (_, i) => chunk(String(i + 32))), total: 61, limit: 30, offset: 31,
  });
  expect(merged.offset).toBe(31);
});

function page(start: number, end: number, total: number) {
  return { items: Array.from({ length: end - start + 1 }, (_, i) => chunk(String(start + i))),
    offset: start - 1, total, limit: end - start + 1 };
}

it('fills disjoint windows in chronological order without hiding the gap', () => {
  let window = mergeHistoryWindow(undefined, page(1, 30, 30));
  window = mergeHistoryWindow(window, page(71, 100, 100));
  expect(window.offset).toBe(70);
  window = mergeHistoryWindow(window, page(41, 70, 100));
  expect(window.offset).toBe(40);
  window = mergeHistoryWindow(window, page(11, 40, 100));
  expect(window.offset).toBe(0);
  expect(window.items.map(item => item.id)).toEqual(Array.from({ length: 100 }, (_, i) => String(i + 1)));
});

it('does not let an older in-flight response shrink the known total', () => {
  const window = mergeHistoryWindow(mergeHistoryWindow(undefined, page(71, 100, 100)), page(41, 70, 70));
  expect(window.total).toBe(100);
  expect(window.offset).toBe(40);
});

it('counts hidden control rows as loaded coverage, not missing messages', () => {
  const window = mergeHistoryWindow(undefined, { items: [chunk('visible')], total: 30, offset: 0, limit: 30 });
  expect(window.offset).toBe(0);
  expect(window.items).toHaveLength(1);
});

it('preserves durable order when overlapping pages omit hidden rows', () => {
  const previous = mergeHistoryWindow(undefined, {
    items: [{ ...chunk('last'), history_position: 4 }], total: 5, offset: 3, limit: 2,
  });
  const next = mergeHistoryWindow(previous, {
    items: [{ ...chunk('first'), history_position: 1 }, { ...chunk('middle'), history_position: 3 }],
    total: 5, offset: 0, limit: 4,
  });
  expect(next.items.map(row => row.id)).toEqual(['first', 'middle', 'last']);
  expect(next.offset).toBe(0);
});

it('orders synthetic interaction cards by time without increasing pagination coverage', () => {
  const next = mergeHistoryWindow(undefined, {
    items: [{ ...chunk('new'), ts: 20, history_position: 0 }, { ...chunk('approval'), ts: 10 }],
    total: 1, offset: 0, limit: 30,
  });
  expect(next.items.map(row => row.id)).toEqual(['approval', 'new']);
  expect(next.offset).toBe(0);
  expect(next.total).toBe(1);
});
