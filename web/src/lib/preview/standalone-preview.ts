import type { InstanceWorkflowSource } from './instance-workflow';
import { appendPreviewOrigin, type PreviewOrigin } from './context-origin';
import { getBasePath } from '@/lib/base-path';
import type { FileRefV1 } from '@/lib/preview/protocol';

export interface StandalonePreviewTarget {
  fileRef: FileRefV1;
  fileType: string;
}

export function standaloneWorkflowPreviewHref(workflowId: string, version: string, origin?: PreviewOrigin | null, source?: InstanceWorkflowSource): string {
  const query = new URLSearchParams({ type: 'workflow', workflowId, version });
  if (source) { query.set("instanceType", source.type); query.set("instanceId", source.id); }
  appendPreviewOrigin(query, origin);
  return `${getBasePath()}/preview?${query}`;
}

export function standaloneWorkflowPreviewTarget(search: URLSearchParams): { workflowId: string; version: string; source?: InstanceWorkflowSource } | null {
  if (search.get('type') !== 'workflow') return null;
  const workflowId = search.get('workflowId')?.trim() ?? '';
  const version = search.get('version') ?? '';
  if (!workflowId || !/^v[1-9]\d*\.sv\d+$/.test(version)) return null;
  const type = search.get('instanceType');
  const id = search.get('instanceId')?.trim();
  if (type !== null || id !== undefined) {
    if (!id || !['task', 'deployment', 'execution'].includes(type ?? '')) return null;
    return { workflowId, version, source: { type: type as InstanceWorkflowSource['type'], id } };
  }
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
  origin?: PreviewOrigin | null,
): string {
  const query = new URLSearchParams({
    scope: fileRef.scope,
    path: fileRef.path,
    fileType,
  });
  appendPreviewOrigin(query, origin);
  if (fileRef.scope === 'project') query.set('projectId', fileRef.projectId);
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
  if (scope === 'project') {
    const projectId = search.get('projectId')?.trim() ?? '';
    if (
      !projectId
      || !['/data/', '/memory/', '/logs/', '/chats/'].some((prefix) => safePath(path, prefix))
    ) return null;
    return {
      fileRef: {
        schemaVersion: 1,
        scope: 'project',
        projectId,
        path: path as Extract<FileRefV1, { scope: 'project' }>['path'],
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

/** Only in-app resource routes may be used as a preview return destination. */
export function previewReturnPath(value: string | null): string | null {
  if (!value || /[\\\r\n]/.test(value)) return null;
  return value === '/' || /^\/(tasks|deployments|workflow|workflow-executions|chat)(?:\/|\?|#|$)/.test(value) ? value : null;
}
