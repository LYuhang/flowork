import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AsyncState } from '@/components/ui/async-state';
import { apiClient } from '@/lib/api/client';
import { WorkflowPreviewRenderer } from './preview/WorkflowPreviewRenderer';

export interface ChatWorkflowViewerProps {
  workflowId: string | null;
  onClose: () => void;
}

/** Resolve the opening version once. Preview never writes the editor's global draft. */
export function ChatWorkflowViewer({ workflowId }: ChatWorkflowViewerProps) {
  const { t } = useTranslation();
  const query = useQuery({
    queryKey: ['workflow-preview-opening-version', workflowId],
    enabled: !!workflowId, gcTime: 0, staleTime: 0,
    refetchOnMount: 'always', refetchOnWindowFocus: false, refetchOnReconnect: false, retry: false,
    queryFn: async () => {
      const { data, error } = await apiClient.GET('/api/v1/workflows/{wf_id}', {
        params: { path: { wf_id: workflowId! } },
      });
      if (error) throw error;
      return data;
    },
  });
  if (!workflowId) return null;
  if (query.isPending || query.isFetching) return <AsyncState kind="loading" title={t('chat.preview.loadingWorkflow', 'Loading workflow...')} />;
  if (query.isError || query.data?.meta?.active_v == null || query.data.meta.active_sv == null) {
    return <AsyncState kind="error" title={t('preview.workflow.unavailable', 'This workflow version is unavailable or you no longer have access.')} />;
  }
  return <WorkflowPreviewRenderer workflowId={workflowId}
    version={`v${query.data.meta.active_v}.sv${query.data.meta.active_sv}`} inspectorPlacement="right" />;
}
