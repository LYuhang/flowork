import type { VfsReadOut } from '@/lib/api/vfs';

export function JsonRenderer({ entry }: { entry: VfsReadOut }) {
  let pretty = entry.content;
  try {
    pretty = JSON.stringify(JSON.parse(entry.content ?? ''), null, 2);
  } catch {
    // leave raw if not valid JSON
  }
  return <pre className="overflow-auto whitespace-pre-wrap break-words text-xs">{pretty}</pre>;
}
