/**
 * VFS 2c read-only client. Hand-rolled (the /api/v1/vfs route is not in the
 * generated schema.d.ts yet) — mirrors lib/api/kb.ts. Fold into the typed
 * apiClient after a `codegen:api` run.
 */
import { getApiBase } from '@/lib/base-path';
import type { components } from '@/lib/api/schema';

const BASE = getApiBase();

export interface VfsEntry {
  path: string;
  kind: 'artifact' | 'scratch';
  content_type: string;
  abstract: string;
  size_bytes: number;
  wf_version: string | null;
  last_access: number;
  stale: boolean;
  capabilities: VfsItemCapability[];
}

export type VfsItemCapability = 'read' | 'download' | 'copy_path' | 'rename' | 'delete';
export type VfsRootCapability = 'upload' | 'create_folder' | 'rename' | 'delete';

export interface VfsListOut {
  entries: VfsEntry[];
  root_capabilities: Record<string, VfsRootCapability[]>;
}

/** Run-tier entry (RE-4): no synthesized agent-VFS fields, no object_key. */
export interface VfsRunEntry {
  path: string;
  content_type: string;
  size_bytes: number;
  capabilities: Array<'read' | 'download' | 'copy_path'>;
}

export interface VfsRunListOut {
  entries: VfsRunEntry[];
}

/** Writable VFS upload result — `POST /api/v1/vfs/upload`. */
export interface VfsUploadOut {
  path: string;
  size_bytes: number;
  content_type: string;
  replaced: boolean;
}

// Binary files return a descriptor with no inline text. Keep the nullable
// contract from the generated schema instead of claiming content is a string.
export type VfsReadOut = components['schemas']['VfsReadOut'];

async function authedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const { useAuthStore } = await import('@/stores/auth');
  const token = useAuthStore.getState().token;
  const headers = new Headers(init.headers);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const resp = await fetch(`${BASE}${path}`, { ...init, headers });
  if (resp.status === 401) useAuthStore.getState().handle401();
  return resp;
}

async function jsonOrThrow<T>(resp: Response, label: string): Promise<T> {
  if (!resp.ok) {
    let detail = '';
    try {
      const j = await resp.json();
      if (j && typeof j === 'object' && 'detail' in j) {
        detail = ` — ${JSON.stringify((j as { detail: unknown }).detail)}`;
      }
    } catch {
      // non-JSON body
    }
    throw new Error(`${label} failed: ${resp.status} ${resp.statusText}${detail}`);
  }
  return (await resp.json()) as T;
}

function qs(params: Record<string, string | undefined>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) sp.set(k, v);
  const s = sp.toString();
  return s ? `?${s}` : '';
}

export async function listVfs(args: {
  wf_id?: string;
  prefix?: string;
  include_hidden?: string;
}): Promise<VfsListOut> {
  const resp = await authedFetch(`/api/v1/vfs${qs(args)}`);
  return jsonOrThrow<VfsListOut>(resp, 'listVfs');
}

export async function listVfsRun(runId: string): Promise<VfsRunListOut> {
  const resp = await authedFetch(`/api/v1/vfs/runs/${encodeURIComponent(runId)}`);
  // A Workflow uses its stable wfId as the run-tier id. Before that Workflow
  // has executed for the first time there is deliberately no VFS_RUN
  // authorization object or row yet, so the API fails closed with 404. For a
  // list operation that state is semantically an empty directory, not a
  // broken Explorer. Normalizing only the list response also stops TanStack
  // Query from retrying the expected pre-run 404 every polling interval;
  // content/sign requests remain strict 404s.
  if (resp.status === 404) return { entries: [] };
  return jsonOrThrow<VfsRunListOut>(resp, 'listVfsRun');
}

export async function readVfs(args: {
  path: string;
  wf_id?: string;
}): Promise<VfsReadOut> {
  const resp = await authedFetch(`/api/v1/vfs/content${qs(args)}`);
  return jsonOrThrow<VfsReadOut>(resp, 'readVfs');
}

/** Run-tier file content — same endpoint, scoped by `run_id` instead of wf/chat. */
export async function readVfsRun(args: { path: string; run_id: string }): Promise<VfsReadOut> {
  const resp = await authedFetch(`/api/v1/vfs/content${qs(args)}`);
  return jsonOrThrow<VfsReadOut>(resp, 'readVfsRun');
}

/**
 * `POST /api/v1/vfs/sign` result — a short-lived (~5min) signed URL pointing at
 * `GET /api/v1/vfs/raw?...&sig=...` that serves the file BYTES with NO auth
 * header (so it can be used directly as an `<img src>` / `<video src>`).
 */
export interface VfsSignOut {
  url: string;
}

/**
 * Mint a signed bytes URL for a VFS path. Pass `run_id` for `/run/...` files
 * or `wf_id` for durable `/mount|/data/...` files.
 * The tenant is taken from the auth context server-side, never the client.
 */
export async function signVfs(args: {
  path: string;
  wf_id?: string;
  run_id?: string;
}): Promise<VfsSignOut> {
  const resp = await authedFetch('/api/v1/vfs/sign', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(args),
  });
  return jsonOrThrow<VfsSignOut>(resp, 'signVfs');
}

/**
 * Delete a durable VFS path — `DELETE /api/v1/vfs?path=&wf_id=`. Allowlist is
 * paths owned by the current `/mount` or `/data` scope; a folder path deletes
 * all its children.
 * Returns the number of deleted entries (404 when nothing matched).
 */
export interface VfsDeleteOut {
  deleted: number;
}

export async function deleteVfs(args: { path: string; wf_id: string }): Promise<VfsDeleteOut> {
  const resp = await authedFetch(`/api/v1/vfs${qs({ path: args.path, wf_id: args.wf_id })}`, {
    method: 'DELETE',
  });
  return jsonOrThrow<VfsDeleteOut>(resp, 'deleteVfs');
}

/**
 * Rename / move a durable VFS path — `POST /api/v1/vfs/rename`. Same
 * scope-root allowlist; works on a file OR a folder. Returns the new path.
 */
export interface VfsRenameOut {
  path: string;
}

export async function renameVfs(args: {
  wf_id: string;
  old_path: string;
  new_path: string;
}): Promise<VfsRenameOut> {
  const resp = await authedFetch('/api/v1/vfs/rename', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(args),
  });
  return jsonOrThrow<VfsRenameOut>(resp, 'renameVfs');
}

/** Depth-0 VFS folders that accept user uploads. */
export type VfsUploadFolder = 'mount' | 'data';

/**
 * Upload a user file into one writable VFS namespace. `folder` is the depth-0
 * target (`mount` for user-shared files or `data` for one Chat workspace).
 */
export async function uploadVfsFile(
  wfId: string,
  file: File,
  folder: VfsUploadFolder,
): Promise<VfsUploadOut> {
  const fd = new FormData();
  fd.append('file', file);
  const resp = await authedFetch(
    `/api/v1/vfs/upload${qs({ wf_id: wfId, folder })}`,
    { method: 'POST', body: fd },
  );
  return jsonOrThrow<VfsUploadOut>(resp, 'uploadVfsFile');
}
