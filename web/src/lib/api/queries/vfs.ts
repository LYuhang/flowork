import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  deleteVfs,
  listVfs,
  listVfsRun,
  readVfs,
  readVfsRun,
  renameVfs,
  uploadVfsFile,
} from '@/lib/api/vfs';
import type { VfsUploadFolder } from '@/lib/api/vfs';

/** Files list — polled every 3s ONLY while the Explorer is open (enabled). */
export const useVfsList = (
  wfId: string | undefined,
  opts: { enabled: boolean },
) =>
  useQuery({
    queryKey: ['vfs', 'list', wfId],
    queryFn: () => listVfs({ wf_id: wfId }),
    enabled: opts.enabled && !!wfId,
    refetchInterval: 3000,
    refetchIntervalInBackground: false,
  });

/**
 * Run-tier (WORKFLOW_SANDBOX) file list for a debug exec — `GET
 * /api/v1/vfs/runs/{runId}`. Disabled while `runId` is falsy. Polled like
 * the agent-VFS list so a still-running execution surfaces new files.
 */
export const useVfsRunList = (
  runId: string | null,
  opts: { poll?: boolean } = {},
) =>
  useQuery({
    queryKey: ['vfs', 'run-list', runId],
    queryFn: () => listVfsRun(runId as string),
    enabled: !!runId,
    refetchInterval: opts.poll ? 3000 : false,
    refetchIntervalInBackground: false,
  });

/** Single file content — fetched when a file modal opens; not polled. */
export const useVfsContent = (
  wfId: string | undefined,
  path: string | null,
) =>
  useQuery({
    queryKey: ['vfs', 'content', wfId, path],
    queryFn: () => readVfs({ path: path as string, wf_id: wfId }),
    enabled: !!path,
  });

/** Run-tier (WORKFLOW_SANDBOX) file content — scoped by `runId`. */
export const useVfsRunContent = (runId: string | null, path: string | null) =>
  useQuery({
    queryKey: ['vfs', 'run-content', runId, path],
    queryFn: () => readVfsRun({ path: path as string, run_id: runId as string }),
    enabled: !!path && !!runId,
  });

/** A node's persisted per-run result, as written to the run-tier file. */
export interface RunNodeResult {
  inputs?: unknown;
  output?: unknown;
  status?: string;
  error?: string;
}

/**
 * Read one node's persisted run result from the run-tier file
 * `/run/__exec__/nodes/{nodeId}.json` (Task 1 writes the JSON
 * `{node_id, node_name, node_type, status, inputs, output, error, ...}`).
 *
 * Returns the parsed `{ inputs, output, status, error }` (the fields the
 * sider needs) or `undefined` when the file is missing / not yet readable.
 * Enabled only when both `runId` and `nodeId` are present.
 */
export const useRunNodeResult = (
  runId: string | null | undefined,
  nodeId: string,
): RunNodeResult | undefined => {
  const q = useQuery({
    queryKey: ['vfs', 'run-node-result', runId, nodeId],
    queryFn: async (): Promise<RunNodeResult | undefined> => {
      const out = await readVfsRun({
        path: `/run/__exec__/nodes/${nodeId}.json`,
        run_id: runId as string,
      });
      try {
        if (typeof out.content !== 'string') return undefined;
        const parsed = JSON.parse(out.content) as Record<string, unknown>;
        return {
          inputs: parsed.inputs,
          output: parsed.output,
          status: typeof parsed.status === 'string' ? parsed.status : undefined,
          error: typeof parsed.error === 'string' ? parsed.error : undefined,
        };
      } catch {
        return undefined;
      }
    },
    enabled: !!runId && !!nodeId,
    // A missing file 404s — don't hammer it.
    retry: false,
    // The execution streams explicitly invalidate this query whenever a
    // terminal node overwrites the workflow-scoped file. Between writes keep
    // it fresh forever so inspector tab switches do not repeatedly re-fetch +
    // re-parse a potentially large JSON payload.
    staleTime: Infinity,
    gcTime: Infinity,
  });
  return q.data;
};

/** Last workflow-level inputs staged for a run (`/run/__exec__/inputs.json`). */
export const useRunWorkflowInputs = (
  runId: string | null | undefined,
): Record<string, unknown> | undefined => {
  const q = useQuery({
    queryKey: ['vfs', 'run-workflow-inputs', runId],
    queryFn: async (): Promise<Record<string, unknown> | null> => {
      try {
        const out = await readVfsRun({
          path: '/run/__exec__/inputs.json',
          run_id: runId as string,
        });
        if (typeof out.content !== 'string') return null;
        const parsed = JSON.parse(out.content) as unknown;
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
          return null;
        }
        return parsed as Record<string, unknown>;
      } catch {
        return null;
      }
    },
    enabled: !!runId,
    retry: false,
    staleTime: Infinity,
    gcTime: Infinity,
  });
  return q.data ?? undefined;
};

/**
 * Upload a file into one writable depth-0 VFS folder. On success, invalidate
 * the scope's VFS list so the file appears in Explorer.
 */
export const useUploadVfsFile = (
  wfId: string | undefined,
  folder: VfsUploadFolder,
) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => uploadVfsFile(wfId as string, file, folder),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['vfs', 'list', wfId] });
    },
  });
};

/**
 * Delete a durable VFS path (file or folder). On success, invalidate every
 * `['vfs', ...]` query so the Explorer tree drops the removed entries.
 */
export const useDeleteVfs = (wfId: string | undefined) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (path: string) => deleteVfs({ path, wf_id: wfId as string }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['vfs'] });
    },
  });
};

/**
 * Rename / move a durable VFS path. On success, invalidate every `['vfs', ...]`
 * query so the Explorer tree reflects the new path.
 */
export const useRenameVfs = (wfId: string | undefined) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: { old_path: string; new_path: string }) =>
      renameVfs({ wf_id: wfId as string, old_path: args.old_path, new_path: args.new_path }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['vfs'] });
    },
  });
};
