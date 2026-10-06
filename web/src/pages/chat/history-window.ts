import type { RawChunk } from '@/components/agent-sidebar/types';
import type { ChatHistoryPage } from '@/lib/api/queries/chats';

export type ChatHistoryWindow = Omit<ChatHistoryPage, 'items'> & { items: RawChunk[] };

function historyChunkKey(chunk: RawChunk): string {
  if (chunk.id) return `id:${chunk.id}`;
  if (chunk.tool_call_id) return `tool:${chunk.tool_call_id}:${chunk.content.length}`;
  return `${chunk.role}:${chunk.ts ?? ''}:${chunk.content.slice(0, 120)}:${chunk.content.length}`;
}

/** Merge durable history pages without moving an older page behind the tail. */
export function mergeHistoryWindow(
  previous: ChatHistoryWindow | undefined,
  page: ChatHistoryPage,
): ChatHistoryWindow {
  const pageItems = page.items as RawChunk[];
  // An empty pre-Turn checkpoint covers no history. Its zero offset must
  // not hide pagination when the first completed Turn exceeds one page.
  if (!previous || previous.total === 0) return { ...page, items: pageItems };

  // A smaller offset is an older page and must be prepended. A same/newer
  // offset is a refreshed tail and must be appended so its canonical rows win
  // if a tool message changed from running to terminal between reads.
  const candidates = page.offset < previous.offset
    ? [...pageItems, ...previous.items]
    : [...previous.items, ...pageItems];
  const indexes = new Map<string, number>();
  const items: RawChunk[] = [];
  for (const item of candidates) {
    const key = historyChunkKey(item);
    const index = indexes.get(key);
    if (index === undefined) {
      indexes.set(key, items.length);
      items.push(item);
    } else {
      items[index] = item;
    }
  }
  return {
    items,
    total: page.total,
    limit: items.length,
    offset: Math.min(previous.offset, page.offset),
  };
}
