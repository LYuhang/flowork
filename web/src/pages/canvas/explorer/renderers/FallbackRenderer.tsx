import { useTranslation } from 'react-i18next';
import type { VfsReadOut } from '@/lib/api/vfs';
import { formatBytes } from '@/lib/format/bytes';

export function FallbackRenderer({ entry }: { entry: VfsReadOut }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-2">
      <div className="text-xs text-muted-foreground">
        {entry.content_type} · {formatBytes(entry.size_bytes)}
      </div>
      <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-muted p-2 text-xs">
        {typeof entry.content === 'string'
          ? entry.content.slice(0, 4000)
          : t('vfs.no_text_preview', 'This file has no text preview.')}
      </pre>
      <div className="text-xs text-muted-foreground">
        {t('vfs.no_formatted_view', 'No formatted view for this type — showing raw preview.')}
      </div>
    </div>
  );
}
