import { useTranslation } from 'react-i18next';
import { lazy, Suspense } from 'react';

import { AsyncState } from '@/components/ui/async-state';
import { StatusBadge } from '@/components/ui/status';
import type { VfsReadOut } from '@/lib/api/vfs';
import { formatBytes } from '@/lib/format/bytes';
import { resolveFileCapability } from '@/lib/files/capabilities';
import { renderVfsContent } from '@/pages/canvas/explorer/renderers';
import { fileRefFromAgentPath } from '@/lib/preview/protocol';

const FilePreview = lazy(() => import('@/pages/chat/preview/ChatFilePreview').then(
  (module) => ({ default: module.ChatFilePreview }),
));

export function VfsFilePreviewContent({
  data,
  loading,
  error,
  onRetry,
  runId,
}: {
  data?: VfsReadOut;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
  runId?: string | null;
}) {
  const { t } = useTranslation();
  if (loading) return <AsyncState kind="loading" title={t('vfs.loading', 'Loading…')} />;
  if (error || !data) {
    return (
      <AsyncState
        kind="error"
        title={t('vfs.load_error', 'Failed to load file.')}
        actionLabel={t('retry', 'Retry')}
        onAction={onRetry}
      />
    );
  }
  const capability = resolveFileCapability(data.path, data.content_type);
  const fileRef = fileRefFromAgentPath(data.path, { runId: runId ?? data.run_id });
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <StatusBadge status="neutral" showDot={false}>{capability.label}</StatusBadge>
        <span>{formatBytes(data.size_bytes)}</span>
        <span className="select-all font-mono">{data.path}</span>
      </div>
      {data.stale ? (
        <div className="border-y border-state-warning/30 bg-state-warning/10 px-3 py-2 text-xs text-state-warning">
          {t('vfs.stale', 'This file was produced at an earlier workflow version — it may be out of date; re-run to refresh.')}
        </div>
      ) : null}
      {data.truncated ? (
        <div className="border-y border-edge-subtle bg-surface-sunken px-3 py-2 text-xs text-muted-foreground">
          {t('vfs.truncated', 'Large file — content truncated for display.')}
        </div>
      ) : null}
      {typeof data.content !== 'string' && fileRef ? (
        <div className="h-[60vh] min-h-64">
          <Suspense fallback={<AsyncState kind="loading" title={t('vfs.loading', 'Loading…')} />}>
            <FilePreview key={data.path} fileRef={fileRef} allowEditing={false} />
          </Suspense>
        </div>
      ) : renderVfsContent(data)}
    </div>
  );
}
