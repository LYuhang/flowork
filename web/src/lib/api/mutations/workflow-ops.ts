/**
 * Workflow lifecycle mutations — commit / check / new-major-version /
 * server-side undo / server-side redo.
 *
 * Sibling of `workflows.ts` (which holds workspace-level CRUD). These
 * five live on a separate file because they are scoped to a single
 * already-loaded workflow (`wfId`) and are typically invoked from the
 * `CanvasToolbar` rather than from the workspace.
 *
 * Conventions match `workflows.ts`:
 *   - `mutationFn` throws on `error` so TanStack Query lands in `onError`.
 *   - Mutations that change committed state invalidate
 *     `['workflow', wfId]` so `useWorkflow` refetches.
 *   - All error toasts go through the shared {@link errorMessage} helper.
 *
 * Note on undo/redo: the canvas toolbar wires its Undo/Redo buttons to
 * the *local* `useWorkflowEditStore.undo` / `.redo` (in-memory linear
 * history of the in-flight draft). The server-side hooks here exist for
 * later phases (e.g. T14 history pane) that need to move the committed
 * HEAD on the backend; they intentionally have no UI consumer in T7.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { apiClient } from '@/lib/api/client';
import {
  errorMessage,
  isAuthorizationChangedError,
} from '@/lib/api/mutations/error-message';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import { normalizeForSend } from '@/lib/workflow/normalize';
import { workflowVersionsQueryKey } from '@/lib/api/queries/workflow';
import type { components } from '@/lib/api/schema';
import i18n from '@/lib/i18n';

type WorkflowDraft = components['schemas']['CommitRequest']['workflow'];
type CheckResponse = components['schemas']['CheckResponse'];

/**
 * Commit the current draft as a new sub-version.
 *
 * `targetMajor` (UX-5, editable historical versions): when the canvas is
 * pinned to a HISTORICAL major (the `:vKey` route), pass that major so the
 * commit lands under it (its sv grows + HEAD moves onto it) instead of the
 * active major. Omit (the normal active-editing case) to keep the legacy
 * contract — the backend defaults `target_major` to `null`.
 *
 * The response is `WorkflowMetaOut`, so `data.active_v` / `data.active_sv`
 * carry the just-saved (major, sub) — callers on a pinned route use these to
 * navigate to the new vKey.
 */
export const useCommitWorkflow = (wfId: string, targetMajor?: number | null) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (workflow: WorkflowDraft) => {
      const { data, error } = await apiClient.POST(
        '/api/v1/workflows/{wf_id}/commits',
        {
          params: { path: { wf_id: wfId } },
          body: {
            workflow: normalizeForSend(workflow),
            note: '',
            ...(targetMajor != null ? { target_major: targetMajor } : {}),
          },
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      // Re-baseline the draft so `isDirty()` re-derives to clean (the
      // markSaved fix — today Save never cleared dirty). Done BEFORE the
      // invalidate so the server echo of our own commit (draft bytes ==
      // server bytes) reconciles as clean, not as an agent conflict.
      useWorkflowEditStore.getState().markSaved();
      qc.invalidateQueries({ queryKey: ['workflow', wfId] });
      qc.invalidateQueries({ queryKey: workflowVersionsQueryKey(wfId) });
      toast.success(i18n.t('workflow.save.success', 'Saved'));
    },
    onError: (e) => {
      if (isAuthorizationChangedError(e)) {
        // Never clear/re-baseline the draft here. The user keeps their local
        // unsaved input while the backend remains the authority that rejects
        // writes after a live access revocation.
        toast.error(
          i18n.t('workflow.save.accessChanged', 'Your access changed. This draft is still available locally, but it cannot be saved.'),
          { duration: 8_000 },
        );
        return;
      }
      toast.error(i18n.t('workflow.save.failed', 'Save failed: {{message}}', { message: errorMessage(e) }));
    },
  });
};

export const useCheckWorkflow = (wfId: string) => {
  return useMutation({
    // Pass the in-progress DRAFT so Check validates unsaved edits (the user
    // shouldn't have to Save before Check). Omit → backend checks the committed
    // version.
    mutationFn: async (
      workflow?: Record<string, unknown> | null,
    ): Promise<CheckResponse> => {
      const { data, error } = await apiClient.POST(
        '/api/v1/workflows/{wf_id}/check',
        {
          params: { path: { wf_id: wfId } },
          ...(workflow
            ? { body: { workflow: normalizeForSend(workflow) } }
            : {}),
        },
      );
      if (error) throw error;
      return data;
    },
    onError: (e) => {
      toast.error(`Check failed: ${errorMessage(e)}`);
    },
  });
};

export const useNewMajorVersion = (wfId: string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (workflow: WorkflowDraft) => {
      const { data, error } = await apiClient.POST(
        '/api/v1/workflows/{wf_id}/major-versions',
        {
          params: { path: { wf_id: wfId } },
          body: { workflow, note: 'New major version' },
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['workflow', wfId] });
      qc.invalidateQueries({ queryKey: workflowVersionsQueryKey(wfId) });
      toast.success(i18n.t('workflow.version.majorCreated', 'New major version created'));
    },
    onError: (e) => {
      toast.error(`New major version failed: ${errorMessage(e)}`);
    },
  });
};
