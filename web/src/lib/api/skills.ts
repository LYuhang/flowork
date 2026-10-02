import { getApiBase } from '@/lib/base-path';
import type {
  ResourceAccess,
  ResourceProvenance,
} from '@/lib/api/organizations';

export type SkillCatalogSource = 'openai' | 'anthropic';
export type SkillSource = SkillCatalogSource | 'custom';

export interface Skill {
  id: string;
  name: string;
  description: string;
  allowed_tools: string[];
  version: number;
  source?: SkillSource | null;
  source_id?: string | null;
  source_url?: string | null;
  source_revision?: string | null;
  revision_hash?: string | null;
  created_at: string | null;
  updated_at: string | null;
  access: ResourceAccess;
  provenance: ResourceProvenance;
}

export interface SkillDetail extends Skill {
  body: string;
  skill_md: string;
  files: string[];
  has_draft: boolean;
  draft_updated_at: string | null;
}

export interface SkillDraft {
  skill_id: string;
  base_revision_hash: string;
  draft_hash: string | null;
  skill_md: string;
  body: string;
  files: string[];
  has_changes: boolean;
  updated_at: string | null;
  access: ResourceAccess;
  provenance: ResourceProvenance;
}

export interface SkillRevision {
  revision_id: string;
  revision_hash: string;
  version: number;
  is_latest: boolean;
  files: string[];
  size_bytes: number;
  created_at: string | null;
  access: ResourceAccess;
  provenance: ResourceProvenance;
}

export interface SkillRevisionDetail extends SkillRevision {
  name: string;
  description: string;
  allowed_tools: string[];
  skill_md: string;
  body: string;
}

export interface SkillCatalogFile {
  path: string;
  size_bytes: number;
}

export interface SkillCatalogItem {
  source: SkillCatalogSource;
  source_label: string;
  source_id: string;
  name: string;
  description: string;
  version: number;
  allowed_tools: string[];
  homepage: string;
  revision: string;
  files: SkillCatalogFile[];
  skill_md?: string;
  body?: string;
}

export interface SkillCatalogResult {
  source: SkillCatalogSource;
  source_label: string;
  revision: string;
  items: SkillCatalogItem[];
  has_more: boolean;
}

const BASE = getApiBase();
const CATALOG_TIMEOUT_MS = 25_000;

function withCatalogTimeout(init: RequestInit = {}): { init: RequestInit; cleanup: () => void } {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), CATALOG_TIMEOUT_MS);
  const cleanup = () => window.clearTimeout(timeout);
  if (init.signal) init.signal.addEventListener('abort', () => controller.abort(), { once: true });
  return { init: { ...init, signal: controller.signal }, cleanup };
}

async function catalogFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const bounded = withCatalogTimeout(init);
  try {
    return await authedFetch(path, bounded.init);
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new Error(
        'The Skill catalog did not respond in time. Check the catalog connection and try again.',
        { cause: error },
      );
    }
    throw error;
  } finally {
    bounded.cleanup();
  }
}

async function authedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const { useAuthStore } = await import('@/stores/auth');
  const token = useAuthStore.getState().token;
  const headers = new Headers(init.headers);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  // Let the browser add the multipart boundary for file uploads. Setting
  // application/json on FormData makes FastAPI treat the request as having no
  // multipart fields at all.
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await fetch(`${BASE}${path}`, { ...init, headers });
  if (response.status === 401) useAuthStore.getState().handle401();
  return response;
}

async function errorDetail(response: Response): Promise<string> {
  try {
    const payload = await response.json();
    if (payload && typeof payload === 'object' && 'detail' in payload) {
      return String((payload as { detail: unknown }).detail);
    }
  } catch {
    // Non-JSON upstream errors use the HTTP status below.
  }
  return `${response.status} ${response.statusText}`;
}

async function jsonOrThrow<T>(response: Response, label: string): Promise<T> {
  if (!response.ok) throw new Error(`${label} failed: ${await errorDetail(response)}`);
  return (await response.json()) as T;
}

export async function listSkills(): Promise<Skill[]> {
  const response = await authedFetch('/api/v1/skills');
  return (await jsonOrThrow<{ items: Skill[] }>(response, 'listSkills')).items;
}

export async function getSkill(id: string): Promise<SkillDetail> {
  const response = await authedFetch(`/api/v1/skills/${id}`);
  return jsonOrThrow<SkillDetail>(response, 'getSkill');
}

export async function deleteSkill(id: string): Promise<void> {
  const response = await authedFetch(`/api/v1/skills/${id}`, { method: 'DELETE' });
  if (!response.ok) throw new Error(`deleteSkill failed: ${await errorDetail(response)}`);
}

export async function searchSkillCatalog(
  source: SkillCatalogSource,
  search = '',
  limit = 40,
): Promise<SkillCatalogResult> {
  const params = new URLSearchParams({ source, search, limit: String(limit) });
  const response = await catalogFetch(`/api/v1/skills/catalog?${params}`);
  return jsonOrThrow<SkillCatalogResult>(response, 'searchSkillCatalog');
}

export async function resolveSkillCatalogItem(
  source: SkillCatalogSource,
  sourceId: string,
): Promise<SkillCatalogItem> {
  const params = new URLSearchParams({ source, source_id: sourceId });
  const response = await catalogFetch(`/api/v1/skills/catalog/resolve?${params}`);
  return jsonOrThrow<SkillCatalogItem>(response, 'resolveSkillCatalogItem');
}

export async function installSkillCatalogItem(
  source: SkillCatalogSource,
  sourceId: string,
): Promise<Skill> {
  const response = await authedFetch('/api/v1/skills/catalog/install', {
    method: 'POST',
    body: JSON.stringify({ source, source_id: sourceId }),
  });
  return jsonOrThrow<Skill>(response, 'installSkillCatalogItem');
}

export async function saveCustomSkill(input: { bundle: File }): Promise<Skill> {
  const form = new FormData();
  form.append('bundle', input.bundle);
  const response = await authedFetch('/api/v1/skills/custom', {
    method: 'POST',
    body: form,
  });
  return jsonOrThrow<Skill>(response, 'saveCustomSkill');
}

export async function getSkillDraft(id: string): Promise<SkillDraft> {
  const response = await authedFetch(`/api/v1/skills/${encodeURIComponent(id)}/draft`);
  return jsonOrThrow<SkillDraft>(response, 'getSkillDraft');
}

export async function saveSkillDraft(id: string, skillMd: string, expectedHash?: string): Promise<SkillDraft> {
  const response = await authedFetch(`/api/v1/skills/${encodeURIComponent(id)}/draft`, {
    method: 'PUT',
    body: JSON.stringify({ skill_md: skillMd, expected_hash: expectedHash }),
  });
  return jsonOrThrow<SkillDraft>(response, 'saveSkillDraft');
}

export async function publishSkillVersion(id: string, version: number, expectedHash?: string): Promise<Skill> {
  const response = await authedFetch(`/api/v1/skills/${encodeURIComponent(id)}/versions`, {
    method: 'POST',
    body: JSON.stringify({ version, expected_hash: expectedHash }),
  });
  return jsonOrThrow<Skill>(response, 'publishSkillVersion');
}

export async function listSkillVersions(id: string): Promise<SkillRevision[]> {
  const response = await authedFetch(`/api/v1/skills/${encodeURIComponent(id)}/versions`);
  return jsonOrThrow<SkillRevision[]>(response, 'listSkillVersions');
}

export async function getSkillVersion(
  id: string,
  revisionId: string,
): Promise<SkillRevisionDetail> {
  const response = await authedFetch(
    `/api/v1/skills/${encodeURIComponent(id)}/versions/${encodeURIComponent(revisionId)}`,
  );
  return jsonOrThrow<SkillRevisionDetail>(response, 'getSkillVersion');
}

export async function getSkillFile(id: string, path: string): Promise<Blob> {
  const encodedPath = path.split('/').map(encodeURIComponent).join('/');
  const response = await authedFetch(`/api/v1/skills/${id}/files/${encodedPath}`);
  if (!response.ok) throw new Error(`getSkillFile failed: ${await errorDetail(response)}`);
  return response.blob();
}

export async function getSkillVersionFile(
  id: string,
  revisionId: string,
  path: string,
): Promise<Blob> {
  const encodedPath = path.split('/').map(encodeURIComponent).join('/');
  const response = await authedFetch(
    `/api/v1/skills/${encodeURIComponent(id)}/versions/${encodeURIComponent(revisionId)}/files/${encodedPath}`,
  );
  if (!response.ok) throw new Error(`getSkillVersionFile failed: ${await errorDetail(response)}`);
  return response.blob();
}

export async function getCatalogSkillFile(
  source: SkillCatalogSource,
  sourceId: string,
  path: string,
): Promise<Blob> {
  const params = new URLSearchParams({ source, source_id: sourceId, path });
  const response = await authedFetch(`/api/v1/skills/catalog/file?${params}`);
  if (!response.ok) throw new Error(`getCatalogSkillFile failed: ${await errorDetail(response)}`);
  return response.blob();
}

export async function getSkillDraftFile(id: string, path: string): Promise<Blob> {
  const response = await authedFetch(`/api/v1/skills/${encodeURIComponent(id)}/draft/files/${path.split('/').map(encodeURIComponent).join('/')}`);
  if (!response.ok) throw new Error(await errorDetail(response));
  return response.blob();
}

export async function writeSkillDraftFile(id: string, path: string, expectedHash: string, file?: File, create = false): Promise<SkillDraft> {
  const endpoint = `/api/v1/skills/${encodeURIComponent(id)}/draft/files/${path.split('/').map(encodeURIComponent).join('/')}`;
  const body = new FormData();
  if (file) body.append('file', file);
  body.append('expected_hash', expectedHash);
  body.append('create', String(create));
  const response = await authedFetch(file ? endpoint : `${endpoint}?${new URLSearchParams({expected_hash: expectedHash})}`, {
    method: file ? 'PUT' : 'DELETE', ...(file ? { body } : {}),
  });
  return jsonOrThrow<SkillDraft>(response, 'writeSkillDraftFile');
}
