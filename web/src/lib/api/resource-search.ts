import type { components } from './schema';
import { getApiBase } from '@/lib/base-path';
import { useAuthStore } from '@/stores/auth';
import { listTasks } from './tasks';
import { listDeployments } from './deployments';
import i18n from '@/lib/i18n';

export type ResourceSearchKind = 'projects' | 'chats' | 'tasks' | 'deployments';
export type ResourceSearchItem = components['schemas']['TitleSearchItem'];
export type ResourceSearchPage = components['schemas']['TitleSearchPage'];

/** Each call reads a fresh server page, never the currently loaded UI inventory. */
export async function searchResourcePage(
  kind: ResourceSearchKind, query: string, cursor: string | null, signal: AbortSignal,
): Promise<ResourceSearchPage> {
  if (kind === 'projects' || kind === 'chats') {
    const response = await fetch(`${getApiBase()}/api/v1/search/${kind}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, signal,
      body: JSON.stringify({ query, after_id: cursor ?? '' }),
    });
    if (response.status === 401) useAuthStore.getState().handle401();
    if (!response.ok) throw new Error(`Search failed (${response.status})`);
    return response.json();
  }
  // Existing Task q searches identifiers, not encrypted payload.name. Read the
  // authorized projection page and match visible names literally for both kinds.
  const offset = Number(cursor ?? 0);
  const params = { limit: 100, offset };
  const page = kind === 'tasks'
    ? await listTasks(params, signal)
    : await listDeployments(params, signal);
  const items = page.items.map(item => {
    if ('name' in item) return { id: item.id, title: item.name };
    const payload = item.payload as Record<string, unknown> | null;
    const configured = typeof payload?.name === 'string' ? payload.name.trim() : '';
    const title = configured && configured !== 'Scheduled run' && configured !== 'Batch execution'
      ? configured : i18n.t(item.task_type === 'scheduled_run' ? 'tasks.type.scheduled_run' : 'tasks.type.batch_exec');
    return { id: item.id, title };
  });
  return {
    items: items.filter(item => item.title.toLocaleLowerCase().includes(query.toLocaleLowerCase())),
    scanned: page.items.length,
    next_cursor: offset + page.limit < page.total ? String(offset + page.limit) : null,
  };
}
