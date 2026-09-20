import type { VfsReadOut } from '@/lib/api/vfs';

export function LinkRenderer({ entry }: { entry: VfsReadOut }) {
  const url = (entry.content ?? '').trim();
  return (
    <a href={url} target="_blank" rel="noopener noreferrer" className="text-sm text-primary underline">
      {url || '(empty link)'}
    </a>
  );
}
