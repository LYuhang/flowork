/**
 * Knowledge API client for authoritative, versioned file packages.
 *
 * Thin typed wrappers over the Knowledge package endpoints (`routes/kb.py`):
 *
 *   * `POST   /api/v1/kb`                              — create a blank package
 *   * `POST   /api/v1/kb/import`                       — create from a folder or ZIP
 *   * `GET    /api/v1/kb`                              — list KBs (tenant-scoped via FORCE RLS)
 *   * `GET    /api/v1/kb/{id}`                         — KB detail (counts + latest_updated_at)
 *   * `PATCH  /api/v1/kb/{id}`                         — partial update (name, description)
 *   * `DELETE /api/v1/kb/{id}`                         — soft delete (204; 409 if any file indexing)
 *   * `GET    /api/v1/kb/{id}/files?status=...`        — list files (optional status filter alias)
 *   * `POST   /api/v1/kb/{id}/files`                   — upload (multipart/form-data)
 *   * `DELETE /api/v1/kb/{id}/files/{fid}`             — soft delete file (404 if missing — T6 fix #3)
 *   * `POST   /api/v1/kb/search`                       — encrypted grep-style search
 *
 * Mirrors the hand-rolled-types approach in `mcp-servers.ts` /
 * `deployments.ts` — these routes were added after the last
 * `pnpm codegen:api:offline` snapshot, so a follow-up regen will eventually
 * fold them into the typed `apiClient`. Everything funnels through the same
 * per-file `authedFetch` shim (Bearer header + 401 → reopen login).
 *
 * The browser treats raw package files as authoritative. Search is an
 * optional derived capability and has no user-operated maintenance surface.
 */

// ---------------------------------------------------------------------------
// Contract — kept in sync with `routes/kb.py` + migration 007 schema.
// ---------------------------------------------------------------------------

export type KbFileStatus = 'stored' | 'pending' | 'indexing' | 'indexed' | 'failed';

export interface Kb {
  id: string;
  name: string;
  description: string | null;
  summary?: string | null;
  retrieval_strategy: 'agentic_lexical';
  package_version: number;
  created_at: string;
  updated_at: string;
  access: import('@/lib/api/organizations').ResourceAccess;
  provenance: import('@/lib/api/organizations').ResourceProvenance;
}

export interface KbDetail extends Kb {
  file_count: number;
  chunk_count: number;
  /**
   * `max(kb.updated_at, max(file.updated_at))` — drives frontend polling so
   * the detail panel can detect rename / file added / file indexed / file
   * deleted without polling every sub-resource. Restored by the T6 post-fix
   * (commit 663ef67) after an initial omission.
   */
  latest_updated_at: string;
}

export interface KbListItem extends Kb {
  file_count: number;
  chunk_count: number;
  stored_count: number;
  pending_count: number;
  indexing_count: number;
  indexed_count: number;
  failed_count: number;
  latest_updated_at: string;
}

export interface KbFile {
  id: string;
  name: string;
  parser_type: string;
  mime_type: string;
  file_size: number;
  status: KbFileStatus;
  error_message: string | null;
  chunk_count: number;
  created_at: string;
  access: import('@/lib/api/organizations').ResourceAccess;
  provenance: import('@/lib/api/organizations').ResourceProvenance;
}

export interface CreateKbBody {
  name: string;
  description?: string;
}

export type ImportKbSource =
  | { kind: 'folder'; files: File[]; paths: string[] }
  | { kind: 'archive'; file: File };

export interface UpdateKbBody {
  name?: string;
  description?: string;
}

export interface ListKbFilesParams {
  /**
   * Optional server-side filter restored by the T6 post-fix (commit 663ef67).
   * Accepts any `KbFileStatus` value.
   */
  status?: KbFileStatus;
}

export interface UploadKbFileResponse {
  file_id: string;
  task_id: string | null;
  status: string;
}

// ---------------------------------------------------------------------------
// Internal: same `authedFetch` shim as `mcp-servers.ts` / `deployments.ts`.
// Kept file-local on purpose — see the comment in `mcp-servers.ts`. T6 mounts
// the KB router follows the same `/api/v1` namespace as other business APIs.
// ---------------------------------------------------------------------------

import { getApiBase } from '@/lib/base-path';

const BASE = getApiBase();

async function authedFetch(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const { useAuthStore } = await import('@/stores/auth');
  const token = useAuthStore.getState().token;
  const headers = new Headers(init.headers);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  // Only set JSON Content-Type for non-FormData bodies. FormData must let
  // the browser set its own `multipart/form-data; boundary=...` header.
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const resp = await fetch(`${BASE}${path}`, { ...init, headers });
  if (resp.status === 401) {
    useAuthStore.getState().handle401();
  }
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
      // ignore non-JSON bodies
    }
    throw new Error(
      `${label} failed: ${resp.status} ${resp.statusText}${detail}`,
    );
  }
  return (await resp.json()) as T;
}

async function okOrThrow(resp: Response, label: string): Promise<void> {
  if (!resp.ok) {
    let detail = '';
    try {
      const j = await resp.json();
      if (j && typeof j === 'object' && 'detail' in j) {
        detail = ` — ${JSON.stringify((j as { detail: unknown }).detail)}`;
      }
    } catch {
      // ignore non-JSON bodies
    }
    throw new Error(
      `${label} failed: ${resp.status} ${resp.statusText}${detail}`,
    );
  }
}

// ---------------------------------------------------------------------------
// Public API.
// ---------------------------------------------------------------------------

export async function listKbs(): Promise<KbListItem[]> {
  const resp = await authedFetch('/api/v1/kb');
  return jsonOrThrow<KbListItem[]>(resp, 'listKbs');
}

export async function createKb(body: CreateKbBody): Promise<Kb> {
  const resp = await authedFetch('/api/v1/kb', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  return jsonOrThrow<Kb>(resp, 'createKb');
}

export async function importKb(
  body: CreateKbBody,
  source: ImportKbSource,
): Promise<Kb> {
  const form = new FormData();
  form.append('name', body.name);
  if (body.description) form.append('description', body.description);
  if (source.kind === 'archive') {
    form.append('archive', source.file, source.file.name);
  } else {
    source.files.forEach((file, index) => {
      form.append('files', file, file.name);
      form.append('paths', source.paths[index] ?? file.name);
    });
  }
  const resp = await authedFetch('/api/v1/kb/import', {
    method: 'POST',
    body: form,
  });
  return jsonOrThrow<Kb>(resp, 'importKb');
}

export async function getKb(id: string): Promise<KbDetail> {
  const resp = await authedFetch(`/api/v1/kb/${id}`);
  return jsonOrThrow<KbDetail>(resp, 'getKb');
}

export async function updateKb(id: string, body: UpdateKbBody): Promise<Kb> {
  const resp = await authedFetch(`/api/v1/kb/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
  return jsonOrThrow<Kb>(resp, 'updateKb');
}

export async function deleteKb(id: string): Promise<void> {
  const resp = await authedFetch(`/api/v1/kb/${id}`, { method: 'DELETE' });
  await okOrThrow(resp, 'deleteKb');
}

export async function listKbFiles(
  kbId: string,
  params?: ListKbFilesParams,
): Promise<KbFile[]> {
  const qs = params?.status
    ? `?${new URLSearchParams({ status: params.status }).toString()}`
    : '';
  const resp = await authedFetch(`/api/v1/kb/${kbId}/files${qs}`);
  return jsonOrThrow<KbFile[]>(resp, 'listKbFiles');
}

export async function getKbFileRaw(kbId: string, fileId: string): Promise<Blob> {
  const resp = await authedFetch(`/api/v1/kb/${kbId}/files/${fileId}/raw`);
  if (!resp.ok) throw new Error(`getKbFileRaw failed: ${resp.status} ${resp.statusText}`);
  return resp.blob();
}

export async function uploadKbFile(
  kbId: string,
  file: File,
  path?: string,
): Promise<UploadKbFileResponse> {
  const fd = new FormData();
  fd.append('file', file, path ?? file.name);
  const resp = await authedFetch(`/api/v1/kb/${kbId}/files`, {
    method: 'POST',
    body: fd,
  });
  return jsonOrThrow<UploadKbFileResponse>(resp, 'uploadKbFile');
}

export async function deleteKbFile(kbId: string, fileId: string): Promise<void> {
  const resp = await authedFetch(`/api/v1/kb/${kbId}/files/${fileId}`, {
    method: 'DELETE',
  });
  await okOrThrow(resp, 'deleteKbFile');
}

export interface KnowledgeSnapshot {
  version: number;
  base_version: number;
  latest_version: number;
  content_hash: string;
  has_changes: boolean;
  files: string[];
  readme: string;
  updated_at: string;
}

export async function getKnowledgeDraft(id: string): Promise<KnowledgeSnapshot> {
  return jsonOrThrow(await authedFetch(`/api/v1/kb/${id}/draft`), 'getKnowledgeDraft');
}
export async function getKnowledgeVersions(id: string): Promise<Array<{version: number; file_count: number; created_at: string}>> {
  return jsonOrThrow(await authedFetch(`/api/v1/kb/${id}/versions`), 'getKnowledgeVersions');
}
export async function getKnowledgeVersion(id: string, version: number): Promise<KnowledgeSnapshot> {
  return jsonOrThrow(await authedFetch(`/api/v1/kb/${id}/versions/${version}`), 'getKnowledgeVersion');
}
export async function getKnowledgeVersionFile(id: string, version: number, path: string): Promise<Blob> {
  const response = await authedFetch(`/api/v1/kb/${id}/versions/${version}/files/${path.split('/').map(encodeURIComponent).join('/')}`);
  await okOrThrow(response, 'getKnowledgeVersionFile');
  return response.blob();
}
export async function writeKnowledgeDraftFile(id: string, path: string, expectedHash: string, file?: File, create = false): Promise<KnowledgeSnapshot> {
  const endpoint = `/api/v1/kb/${id}/draft/files/${path.split('/').map(encodeURIComponent).join('/')}`;
  const body = new FormData();
  if (file) body.append('file', file);
  body.append('expected_hash', expectedHash);
  body.append('create', String(create));
  return jsonOrThrow(await authedFetch(file ? endpoint : `${endpoint}?${new URLSearchParams({expected_hash: expectedHash})}`, {method: file ? 'PUT' : 'DELETE', ...(file ? {body} : {})}), 'writeKnowledgeDraftFile');
}
export async function publishKnowledgeDraft(id: string, expectedHash: string): Promise<{version: number}> {
  return jsonOrThrow(await authedFetch(`/api/v1/kb/${id}/versions`, {method: 'POST', body: JSON.stringify({expected_hash: expectedHash})}), 'publishKnowledgeDraft');
}
