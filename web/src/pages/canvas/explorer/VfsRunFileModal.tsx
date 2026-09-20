import { useTranslation } from 'react-i18next';
import { AuxiliaryPane } from '@/components/layout/auxiliary-pane';
import { useVfsRunContent } from '@/lib/api/queries/vfs';
import { VfsFilePreviewContent } from './VfsFilePreviewContent';

export interface VfsRunFileModalProps {
  runId: string | null;
  path: string | null;
  onClose: () => void;
}

/** Read-only preview of a run-tier (WORKFLOW_SANDBOX) file. Mirrors
 *  `VfsFileModal` but scopes content by `runId`. HTML-escaped content via
 *  `renderVfsContent` (never dangerouslySetInnerHTML). */
export function VfsRunFileModal({ runId, path, onClose }: VfsRunFileModalProps) {
  const { t } = useTranslation();
  const q = useVfsRunContent(runId, path);
  return (
    <AuxiliaryPane
      open={path !== null}
      title={<span className="block truncate font-mono">{path}</span>}
      closeLabel={t('close', 'Close')}
      resizeLabel={t('vfs.resize_run_preview', 'Resize run file preview')}
      storageKey="vibecanvas:workflow-run-file-preview-width:v1"
      onClose={onClose}
    >
      <VfsFilePreviewContent data={q.data} runId={runId} loading={q.isLoading} error={q.isError} onRetry={() => void q.refetch()} />
    </AuxiliaryPane>
  );
}
