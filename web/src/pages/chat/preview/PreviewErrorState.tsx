import { Download, FileWarning } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { formatBytes } from '@/lib/format/bytes';
import type { PreviewDescriptorV1, PreviewErrorInfo } from '@/lib/preview/protocol';

const ERROR_KEYS: Record<string, string> = {
  file_too_large: 'preview.error.file_too_large',
  too_many_sheets: 'preview.error.too_many_sheets',
  too_many_rows: 'preview.error.too_many_rows',
  too_many_columns: 'preview.error.too_many_columns',
  unsupported_file_type: 'preview.error.unsupported_file_type',
  archive_preview_not_supported: 'preview.error.archive_preview_not_supported',
  encrypted_file: 'preview.error.encrypted_file',
  invalid_file: 'preview.error.invalid_file',
  unsupported_text_encoding: 'preview.error.unsupported_text_encoding',
  too_many_archive_entries: 'preview.error.too_many_archive_entries',
  archive_entry_too_large: 'preview.error.archive_entry_too_large',
  archive_expanded_too_large: 'preview.error.archive_expanded_too_large',
  archive_compression_ratio_too_high: 'preview.error.archive_compression_ratio_too_high',
  content_unavailable: 'preview.error.content_unavailable',
  permission_denied: 'preview.error.permission_denied',
  render_failed: 'preview.error.render_failed',
};

export function PreviewErrorState({
  descriptor,
  error,
  message,
  onRetry,
}: {
  descriptor: PreviewDescriptorV1;
  error?: PreviewErrorInfo | null;
  message?: string;
  onRetry?: () => void;
}) {
  const { t } = useTranslation();
  const details = error ?? descriptor.error ?? { code: 'render_failed', params: {} };
  const actualBytes = details.params.actualBytes;
  const limitBytes = details.params.limitBytes;
  const translationParams = {
    ...details.params,
    actualSize: typeof actualBytes === 'number' ? formatBytes(actualBytes) : '',
    limitSize: typeof limitBytes === 'number' ? formatBytes(limitBytes) : '',
  };
  const messageKey = ERROR_KEYS[details.code] ?? 'preview.error.generic';

  return (
    <div
      role="alert"
      data-preview-error={details.code}
      className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center"
    >
      <FileWarning className="h-9 w-9 text-muted-foreground" />
      <div>
        <div className="font-medium">{descriptor.name}</div>
        <div className="mt-1 text-xs text-muted-foreground">
          {descriptor.detectedType} · {formatBytes(descriptor.sizeBytes)}
        </div>
      </div>
      <div className="max-w-md">
        <div className="text-sm font-medium">
          {t('preview.error.title', 'Preview unavailable')}
        </div>
        <p className="mt-1 text-sm text-muted-foreground">
          {message ?? t(messageKey, {
            ...translationParams,
            defaultValue: t(
              'preview.error.generic',
              'This file cannot be displayed in Preview.',
            ),
          })}
        </p>
      </div>
      {onRetry ? <Button variant="outline" onClick={onRetry}>{t('preview.openError.action', 'Try again')}</Button> : null}
      {descriptor.capabilities.download && descriptor.content?.url ? (
        <Button asChild variant="outline">
          <a href={descriptor.content.url} download={descriptor.name}>
            <Download className="mr-1.5 h-4 w-4" />
            {t('preview.action.download', 'Download')}
          </a>
        </Button>
      ) : null}
    </div>
  );
}
