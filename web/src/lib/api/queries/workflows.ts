/**
 * Workspace listing query — `GET /api/v1/workflows`.
 *
 * The backend returns a `Page[WorkflowMetaOut]` ({items, total, limit, offset}).
 * The query key is `['workspace', {limit, offset}]` so that pagination changes
 * cache separately. Mutations in `mutations/workflows.ts` invalidate the
 * top-level `['workspace']` prefix to refresh every page slice.
 *
 * Live data only: TanStack Query's default 30s `staleTime` (set in
 * `app/providers.tsx`) is fine here — invalidations from create/delete/patch
 * force a refetch regardless of staleness.
 */
import { useQuery } from '@tanstack/react-query';
import { apiClient } from '@/lib/api/client';

export async function fetchWorkspacePage(limit = 50, offset = 0) {
  const { data, error } = await apiClient.GET('/api/v1/workflows', {
    params: { query: { limit, offset } },
  });
  if (error) throw error;
  return data;
}

export const workspaceListQueryOptions = (limit = 50, offset = 0) => ({
    queryKey: ['workspace', { limit, offset }],
    queryFn: () => fetchWorkspacePage(limit, offset),
    staleTime: 30_000,
  } as const);

export const useWorkspaceList = (limit = 50, offset = 0, enabled = true) =>
  useQuery({
    ...workspaceListQueryOptions(limit, offset),
    enabled,
    placeholderData: (previous) => previous,
  });

/**
 * Load the full metadata catalog only for operations that genuinely need it
 * (cross-page search, non-default sort, or sandbox filtering). Normal page
 * entry remains a single small request.
 */
export const useWorkspaceCatalog = (
  enabled: boolean,
  pageSize = 200,
  seed?: { data: Awaited<ReturnType<typeof fetchWorkspacePage>>; updatedAt: number },
) =>
  useQuery({
    queryKey: ['workspace', 'catalog'],
    enabled,
    staleTime: 30_000,
    initialData: seed?.data,
    initialDataUpdatedAt: seed?.updatedAt,
    queryFn: async () => {
      const first = await fetchWorkspacePage(pageSize, 0);
      const offsets: number[] = [];
      for (let offset = pageSize; offset < first.total; offset += pageSize) {
        offsets.push(offset);
      }
      const rest = await Promise.all(
        offsets.map((offset) => fetchWorkspacePage(pageSize, offset)),
      );
      return {
        ...first,
        items: [
          ...first.items,
          ...rest.flatMap((page) => page.items),
        ],
        limit: first.total,
        offset: 0,
      };
    },
  });
