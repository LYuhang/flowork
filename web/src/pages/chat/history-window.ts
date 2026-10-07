import type { RawChunk } from '@/components/agent-sidebar/types';
import type { ChatHistoryPage } from '@/lib/api/queries/chats';

export type ChatHistoryWindow = Omit<ChatHistoryPage, 'items'> & { items: RawChunk[]; positions?: number[]; ranges?: [number, number][] };

function historyChunkKey(chunk: RawChunk): string {
  if (chunk.id) return `id:${chunk.id}`;
  if (chunk.tool_call_id) return `tool:${chunk.tool_call_id}:${chunk.content.length}`;
  return `${chunk.role}:${chunk.ts ?? ''}:${chunk.content.slice(0, 120)}:${chunk.content.length}`;
}

/** Merge durable history pages without moving an older page behind the tail. */
export function mergeHistoryWindow(
  previous: ChatHistoryWindow | undefined,
  page: ChatHistoryPage,
  beforePosition?: number,
): ChatHistoryWindow {
  // Keep absolute positions: retained pages can be disjoint after a long
  // background Turn. A minimum offset cannot describe that coverage.
  const rows = new Map<string, { item: RawChunk; position: number }>();
  previous?.items.forEach((item, index) => {
    const position = previous.positions?.[index] ?? previous.offset + index;
    if (beforePosition !== undefined && position >= beforePosition) return;
    rows.set(historyChunkKey(item), {
      item, position,
    });
  });
  (page.items as RawChunk[]).forEach((item, index) => {
    rows.set(historyChunkKey(item), { item, position: item.history_position ?? page.offset + index });
  });
  const ordered = [...rows.values()].sort((a, b) => {
    // Projection cards do not consume durable positions. Their timestamp
    // places them beside the interaction rather than at each fetched tail.
    if (a.item.ts != null && b.item.ts != null && a.item.ts !== b.item.ts) {
      return a.item.ts - b.item.ts;
    }
    return a.position - b.position;
  });
  const positions = ordered.map(row => row.position);
  const ranges: [number, number][] = [
    ...(previous?.ranges ?? (previous && previous.total > 0
      ? [[previous.offset, Math.min(previous.total, previous.offset + previous.limit)] as [number, number]] : [])),
    [page.offset, Math.min(page.total, page.offset + page.limit)],
  ];
  ranges.sort((a, b) => a[0] - b[0]);
  const coverage: [number, number][] = [];
  for (const sourceRange of ranges) {
    const range: [number, number] = [sourceRange[0], Math.min(sourceRange[1], beforePosition ?? Infinity)];
    if (range[0] >= range[1]) continue;
    const last = coverage.at(-1);
    if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1]);
    else coverage.push([...range]);
  }
  const total = Math.min(Math.max(previous?.total ?? 0, page.total), beforePosition ?? Infinity);
  // The pagination cursor walks backwards from the newest known boundary,
  // stopping at the first gap even if the beginning is already cached.
  let offset = total;
  for (let i = coverage.length - 1; i >= 0; i -= 1) {
    const [start, end] = coverage[i];
    if (end >= offset) offset = Math.min(offset, start);
    else break;
  }
  return {
    items: ordered.map(row => row.item),
    positions,
    ranges: coverage,
    total,
    limit: ordered.length,
    offset,
  };
}
