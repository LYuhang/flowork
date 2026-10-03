/**
 * Workflow CRUD mutations.
 *
 * Each mutation is a thin wrapper around `apiClient` that:
 *   1. Throws on `error` (so TanStack Query lands in `onError`).
 *   2. Returns the parsed body on success.
 *   3. Invalidates the workspace list cache (`['workspace']`) on success so
 *      `WorkspacePage` re-fetches — and, for PATCH, also invalidates the
 *      per-workflow cache key (`['workflow', wfId]`) used by T6 onward.
 *   4. Pops a sonner toast on success/error.
 *
 * The success toast text is fixed; the error toast surfaces a human-readable
 * message via the shared `errorMessage` helper (see `./error-message.ts`).
 * openapi-fetch rejects the raw response body for non-2xx responses —
 * typically a FastAPI `HTTPValidationError` — which is not an `Error`
 * instance, so we coerce common shapes there.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { apiClient } from '@/lib/api/client';
import { errorMessage } from '@/lib/api/mutations/error-message';
import type { components } from '@/lib/api/schema';
import i18n from '@/lib/i18n';

type WorkflowCreate = components['schemas']['WorkflowCreate'];
type WorkflowMetaPatch = components['schemas']['WorkflowMetaPatch'];

export const useCreateWorkflow = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: WorkflowCreate) => {
      const { data, error } = await apiClient.POST('/api/v1/workflows', {
        body,
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['workspace'] });
      toast.success(i18n.t('workflow.toast.created', 'Workflow created'));
    },
    onError: (e) => {
      toast.error(`Create failed: ${errorMessage(e)}`);
    },
  });
};

export const useDeleteWorkflow = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (wfId: string) => {
      const { error } = await apiClient.DELETE(
        '/api/v1/workflows/{wf_id}',
        { params: { path: { wf_id: wfId } } },
      );
      if (error) throw error;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['workspace'] });
      toast.success(i18n.t('workflow.toast.deleted', 'Workflow deleted'));
    },
    onError: (e) => {
      toast.error(`Delete failed: ${errorMessage(e)}`);
    },
  });
};

/**
 * Front-end-only "Duplicate" — there is NO backend duplicate endpoint, so we
 * compose three existing calls:
 *   1. GET  /workflows/{src}            → read the source's current snapshot.
 *   2. POST /workflows                  → create an empty workflow with the
 *      caller-supplied name + description.
 *   3. POST /workflows/{new}/commits    → commit the source dict into it.
 * The new workflow's first commit lands at v1.sv1 carrying the full graph.
 * We invalidate the workspace list on success so the new row appears.
 *
 * `name` is used verbatim (the Duplicate dialog already defaults it to
 * `{orig} (copy)`); `description`, when provided, overrides the source's. Tags
 * are always carried over from the source snapshot.
 */
export interface DuplicateWorkflowVars {
  wfId: string;
  name: string;
  description?: string;
}

export const useDuplicateWorkflow = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ wfId, name, description }: DuplicateWorkflowVars) => {
      // 1. Read the source's current workflow dict.
      const { data: snap, error: snapErr } = await apiClient.GET(
        '/api/v1/workflows/{wf_id}',
        { params: { path: { wf_id: wfId } } },
      );
      if (snapErr) throw snapErr;

      // 2. Create the destination workflow (empty shell).
      const { data: created, error: createErr } = await apiClient.POST(
        '/api/v1/workflows',
        {
          body: {
            name,
            description: description ?? snap?.meta.description ?? '',
            tags: snap?.meta.tags ?? [],
          },
        },
      );
      if (createErr) throw createErr;

      // 3. Commit the source graph into the new workflow.
      const { error: commitErr } = await apiClient.POST(
        '/api/v1/workflows/{wf_id}/commits',
        {
          params: { path: { wf_id: created.wf_id } },
          body: { workflow: snap?.workflow ?? {}, note: 'Duplicated', expected_version: `v${created.active_v}.sv${created.active_sv}` },
        },
      );
      if (commitErr) throw commitErr;

      return created;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['workspace'] });
      toast.success(i18n.t('workflow.toast.duplicated', 'Workflow duplicated'));
    },
    onError: (e) => {
      toast.error(`Duplicate failed: ${errorMessage(e)}`);
    },
  });
};

export interface UpdateWorkflowMetaVars extends WorkflowMetaPatch {
  wfId: string;
}

export const useUpdateWorkflowMeta = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ wfId, ...patch }: UpdateWorkflowMetaVars) => {
      const { data, error } = await apiClient.PATCH(
        '/api/v1/workflows/{wf_id}',
        {
          params: { path: { wf_id: wfId } },
          body: patch,
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ['workflow', vars.wfId] });
      qc.invalidateQueries({ queryKey: ['workspace'] });
      toast.success(i18n.t('workflow.toast.updated', 'Workflow updated'));
    },
    onError: (e) => {
      toast.error(`Update failed: ${errorMessage(e)}`);
    },
  });
};
