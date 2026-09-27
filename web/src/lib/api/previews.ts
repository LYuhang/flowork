import { getApiBase } from '@/lib/base-path';
import type {
  FileRefV1,
  PreviewDescriptorV1,
  PreviewFileWriteOut,
  PreviewResourceSessionV1,
} from '@/lib/preview/protocol';
import { resolveApiUrl } from '@/lib/base-path';
import { fileCitationTarget } from '@/lib/preview/protocol';

const BASE = getApiBase();

export class PreviewApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(
    message: string,
    status: number,
    detail: string,
  ) {
    super(message);
    this.name = 'PreviewApiError';
    this.status = status;
    this.detail = detail;
  }
}

async function authedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const { useAuthStore } = await import('@/stores/auth');
  const token = useAuthStore.getState().token;
  const headers = new Headers(init.headers);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetch(`${BASE}${path}`, { ...init, headers });
  if (response.status === 401) useAuthStore.getState().handle401();
  return response;
}

async function jsonOrThrow<T>(response: Response, label: string): Promise<T> {
  if (response.ok) return response.json() as Promise<T>;
  let detail = response.statusText;
  try {
    const body = await response.json() as { detail?: unknown };
    if (typeof body.detail === 'string') detail = body.detail;
  } catch {
    // Keep the HTTP status text for non-JSON failures.
  }
  throw new PreviewApiError(`${label} failed: ${detail}`, response.status, detail);
}

export async function resolvePreview(
  fileRef: FileRefV1,
  signal?: AbortSignal,
): Promise<PreviewDescriptorV1> {
  const resolve = async (ref: FileRefV1) => jsonOrThrow<PreviewDescriptorV1>(
    await authedFetch('/api/v1/previews/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fileRef: ref }),
      signal,
    }),
    'resolvePreview',
  );
  try {
    return await resolve(fileRef);
  } catch (error) {
    // Old transcripts contain file.md:24 citations. Never reinterpret a real
    // colon filename, auth failure, cancellation, or a service outage.
    const citation = error instanceof PreviewApiError
      && error.status === 404 && error.detail === 'preview_file_not_found'
      ? fileCitationTarget(fileRef)
      : null;
    if (!citation || signal?.aborted) throw error;
    return resolve(citation.fileRef);
  }
}

export async function fetchPreviewRendition(
  url: string,
  signal?: AbortSignal,
): Promise<ArrayBuffer> {
  const response = await authedFetch(url, { signal });
  if (!response.ok) {
    await jsonOrThrow<never>(response, 'fetchPreviewRendition');
  }
  return response.arrayBuffer();
}

export async function writePreviewFile(args: {
  fileRef: FileRefV1;
  expectedRevision: string;
  contentType: string;
  content: string;
}): Promise<PreviewFileWriteOut> {
  const response = await authedFetch('/api/v1/previews/file', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(args),
  });
  return jsonOrThrow<PreviewFileWriteOut>(response, 'writePreviewFile');
}

export async function createPreviewResourceSession(
  fileRef: FileRefV1,
  signal?: AbortSignal,
): Promise<PreviewResourceSessionV1> {
  const response = await authedFetch('/api/v1/previews/resource-session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ fileRef }),
    signal,
  });
  const session = await jsonOrThrow<PreviewResourceSessionV1>(
    response,
    'createPreviewResourceSession',
  );
  return {
    ...session,
    resourceMounts: session.resourceMounts.map((mount) => ({
      ...mount,
      rootUrl: resolveApiUrl(mount.rootUrl),
    })),
    baseUrl: resolveApiUrl(session.baseUrl),
  };
}

/** Resolve one capability-authorized Agent path without accepting remote URLs. */
export function resolvePreviewResourceUrl(
  path: string,
  session: PreviewResourceSessionV1,
): string | null {
  if (
    !path.startsWith('/')
    || path.includes('\\')
    || path.split('/').some((segment) => segment === '.' || segment === '..')
  ) {
    return null;
  }
  const mount = [...session.resourceMounts]
    .sort((left, right) => right.pathPrefix.length - left.pathPrefix.length)
    .find(({ pathPrefix }) => path.startsWith(pathPrefix));
  if (!mount) return null;
  const root = mount.rootUrl.endsWith('/') ? mount.rootUrl : `${mount.rootUrl}/`;
  return `${root}${path.slice(mount.pathPrefix.length).replace(/^\/+/, '')}`;
}
