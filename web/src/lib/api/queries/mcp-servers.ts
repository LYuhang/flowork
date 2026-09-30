/**
 * React-query hooks for the MCP Servers store (Settings → MCP Servers).
 *
 *   - `useMcpServers()`          — list (the picker + manager surface)
 *   - `useMcpServer(id)`         — single row (detail / edit form)
 *   - `useCreateMcpServer()`     — create
 *   - `useUpdateMcpServer()`     — PATCH (partial; may flip `enabled`)
 *   - `useDeleteMcpServer()`     — soft delete
 *   - `useRefreshMcpServer()`    — manual re-handshake
 *   - `useTestMcpServer()`       — dry-run probe (mutation; NEVER invalidates)
 *
 * Mutations invalidate the list (and the touched single-row query for
 * update/refresh) so the UI reflects writes without a manual refetch.
 * Mirrors the structure of `queries/llm-credentials.ts`.
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  createMcpServer,
  deleteMcpServer,
  getMcpServer,
  installMcpCatalogItem,
  listMcpServers,
  resolveMcpCatalogItem,
  refreshMcpServer,
  searchMcpCatalog,
  startMcpOAuth,
  disconnectMcpOAuth,
  testMcpServer,
  updateMcpServer,
} from '@/lib/api/mcp-servers';
import type { McpCatalogSource, McpServer, McpServerInput } from '@/lib/api/mcp-servers';

const LIST_KEY = ['mcp-servers', 'list'] as const;
const itemKey = (id: string) => ['mcp-servers', 'item', id] as const;

/** Tenant-scoped MCP server list. */
export const useMcpServers = (opts?: { enabled?: boolean; refreshOnReturn?: boolean }) =>
  useQuery({
    queryKey: LIST_KEY,
    queryFn: () => listMcpServers(),
    enabled: opts?.enabled ?? true,
    ...(opts?.refreshOnReturn ? {
      refetchOnWindowFocus: 'always' as const,
      refetchOnMount: 'always' as const,
    } : {}),
  });

export const useMcpCatalog = (
  source: McpCatalogSource,
  search: string,
  limit: number,
  opts?: { enabled?: boolean },
) =>
  useQuery({
    queryKey: ['mcp-servers', 'catalog', source, search.trim(), limit],
    queryFn: () => searchMcpCatalog(source, search.trim(), limit),
    enabled: opts?.enabled ?? true,
    placeholderData: keepPreviousData,
    staleTime: 5 * 60 * 1000,
  });

export const useMcpCatalogItem = (
  source: McpCatalogSource | undefined,
  sourceId: string | undefined,
) =>
  useQuery({
    queryKey: ['mcp-servers', 'catalog-item', source ?? '', sourceId ?? ''],
    queryFn: () => resolveMcpCatalogItem(source as McpCatalogSource, sourceId as string),
    enabled: !!source && !!sourceId,
    staleTime: 5 * 60 * 1000,
  });

/** Single MCP server (detail / edit form). Disabled while `id` is falsy. */
export const useMcpServer = (id: string | undefined) =>
  useQuery({
    queryKey: itemKey(id ?? ''),
    queryFn: () => getMcpServer(id as string),
    enabled: !!id,
  });

export const useCreateMcpServer = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: McpServerInput) => createMcpServer(body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: LIST_KEY });
    },
  });
};

export const useInstallMcpCatalogItem = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ source, sourceId }: { source: McpCatalogSource; sourceId: string }) =>
      installMcpCatalogItem(source, sourceId),
    onSuccess: (server) => {
      void qc.invalidateQueries({ queryKey: LIST_KEY });
      qc.setQueryData(itemKey(server.id), server);
    },
  });
};

export const useStartMcpOAuth = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => startMcpOAuth(id),
    onSuccess: (_data, id) => {
      void qc.invalidateQueries({ queryKey: LIST_KEY });
      void qc.invalidateQueries({ queryKey: itemKey(id) });
    },
  });
};

export const useDisconnectMcpOAuth = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => disconnectMcpOAuth(id),
    onSuccess: (_data, id) => {
      void qc.invalidateQueries({ queryKey: LIST_KEY });
      void qc.invalidateQueries({ queryKey: itemKey(id) });
    },
  });
};

export const useUpdateMcpServer = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      patch,
    }: {
      id: string;
      patch: Partial<McpServerInput> & {
        enabled?: boolean;
        description_source?: McpServer['description_source'];
        description_model_id?: string | null;
        description_basis_hash?: string | null;
      };
    }) => updateMcpServer(id, patch),
    onSuccess: (_data, vars) => {
      void qc.invalidateQueries({ queryKey: LIST_KEY });
      void qc.invalidateQueries({ queryKey: itemKey(vars.id) });
    },
  });
};

export const useDeleteMcpServer = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteMcpServer(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: LIST_KEY });
    },
  });
};

export const useRefreshMcpServer = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => refreshMcpServer(id),
    onSuccess: (_data, id) => {
      void qc.invalidateQueries({ queryKey: LIST_KEY });
      void qc.invalidateQueries({ queryKey: itemKey(id) });
    },
  });
};

/**
 * Dry-run handshake probe. A MUTATION (the "Test connection" button calls
 * `.mutateAsync(input)`); it persists nothing and so NEVER invalidates caches.
 */
export const useTestMcpServer = () =>
  useMutation({
    mutationFn: (body: McpServerInput) => testMcpServer(body),
  });
