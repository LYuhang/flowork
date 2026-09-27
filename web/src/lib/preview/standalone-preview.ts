import { getBasePath } from '@/lib/base-path';
import type { FileRefV1 } from '@/lib/preview/protocol';

export interface StandalonePreviewTarget {
  fileRef: FileRefV1;
  fileType: string;
}

export function standaloneWorkflowPreviewHref(workflowId: string, version: string): string {
  return `${getBasePath()}/preview?${new URLSearchParams({ type: 'workflow', workflowId, version })}`;
}

export function standaloneWorkflowPreviewTarget(search: URLSearchParams): { workflowId: string; version: string } | null {
  if (search.get('type') !== 'workflow') return null;
  const workflowId = search.get('workflowId')?.trim() ?? '';
  const version = search.get('version') ?? '';
  if (!workflowId || !/^v[1-9]\d*\.sv\d+$/.test(version)) return null;
  return { workflowId, version };
}

function safePath(path: string, prefix: string): boolean {
  return (
    path.startsWith(prefix)
    && !path.includes('\0')
    && !path.includes('\\')
    && !path.split('/').some((segment) => segment === '.' || segment === '..')
  );
}

/** Build a refresh-safe Preview URL without placing credentials in the URL. */
export function standalonePreviewHref(
  fileRef: FileRefV1,
  fileType = 'auto',
): string {
  const query = new URLSearchParams({
    scope: fileRef.scope,
    path: fileRef.path,
    fileType,
  });
  if (fileRef.scope === 'chat') query.set('chatId', fileRef.chatId);
  if (fileRef.scope === 'run') query.set('runId', fileRef.runId);
  return `${getBasePath()}/preview?${query.toString()}`;
}

/** Parse and validate URL coordinates before issuing any Preview API call. */
export function standalonePreviewTarget(
  search: URLSearchParams,
): StandalonePreviewTarget | null {
  if (search.has('type') && search.get('type') !== 'file') return null;
  const scope = search.get('scope');
  const path = search.get('path') ?? '';
  const fileType = search.get('fileType')?.trim() || 'auto';
  if (scope === 'chat') {
    const chatId = search.get('chatId')?.trim() ?? '';
    if (
      !chatId
      || !['/data/', '/memory/', '/logs/'].some((prefix) => safePath(path, prefix))
    ) return null;
    return {
      fileRef: {
        schemaVersion: 1,
        scope: 'chat',
        chatId,
        path: path as Extract<FileRefV1, { scope: 'chat' }>['path'],
      },
      fileType,
    };
  }
  if (scope === 'mount' && safePath(path, '/mount/')) {
    return {
      fileRef: {
        schemaVersion: 1,
        scope: 'mount',
        path: path as Extract<FileRefV1, { scope: 'mount' }>['path'],
      },
      fileType,
    };
  }
  if (scope === 'run') {
    const runId = search.get('runId')?.trim() ?? '';
    if (!runId || !safePath(path, '/run/')) return null;
    return {
      fileRef: {
        schemaVersion: 1,
        scope: 'run',
        runId,
        path: path as Extract<FileRefV1, { scope: 'run' }>['path'],
      },
      fileType,
    };
  }
  return null;
}
