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
    items: [chunk('recent')], total: 61, limit: 30, offset: 31,
  });
  expect(merged.offset).toBe(31);
});
