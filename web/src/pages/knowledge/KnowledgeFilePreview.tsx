import { useQuery } from '@tanstack/react-query';

import { PackageFileActions, type PackageFileOperations } from '@/components/files/PackageFileActions';
import { useTranslation } from 'react-i18next';
import { FileWorkbenchPreview } from '@/components/files/FileWorkbenchPreview';
import { getKbFileRaw, type KbFile } from '@/lib/api/kb';

export function KnowledgeFilePreview({ kbId, file, operations, revisionKey, loadFile }: { kbId: string; file: KbFile; operations?: PackageFileOperations; revisionKey?: string; loadFile?: (path: string) => Promise<Blob> }) {
  const { t } = useTranslation();
  const query = useQuery({
    queryKey: ['knowledge-file-raw', kbId, file.id, revisionKey],
    queryFn: () => loadFile ? loadFile(file.name) : getKbFileRaw(kbId, file.id),
  });
  return (
    <FileWorkbenchPreview
      actions={operations && <PackageFileActions path={file.name} mimeType={file.mime_type} required={file.name.toLowerCase() === 'readme.md'} operations={operations} savedHint={t('files.manage.knowledgeSaved')} />}
      fileName={file.name}
      mimeType={file.mime_type}
      blob={query.data}
      loading={query.isPending}
      error={query.isError ? query.error.message : null}
    />
  );
}
