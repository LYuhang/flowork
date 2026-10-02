import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Activity } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { ExecutionFrame, ExecutionStatus } from '@/lib/api/queries/workflow-history';
import { NodeTab } from './NodeTab';
import { InspectorSection } from './InspectorSection';
import { NodeJsonPreview } from '../nodes/NodeJsonPreview';
import { historicalNodeStatus } from '../execution-state';

/** Uses only this snapshot and this execution's events, never editor run stores. */
export function ReadOnlyNodeDetails({ workflowId, nodeId, events, executionStatus }: {
  workflowId: string;
  nodeId: string;
  events?: ExecutionFrame[];
  executionStatus?: ExecutionStatus;
}) {
  const { t } = useTranslation();
  const nodeEvents = events?.filter(event => event.type === 'node_event' && event.node_id === nodeId);
  return <Tabs defaultValue="node" className="space-y-4" data-role="readonly-node-details">
    <TabsList variant="underline" className="w-full justify-start">
      <TabsTrigger value="node">{t('inspector.tab.node', 'Node')}</TabsTrigger>
      <TabsTrigger value="run-node">{t('execution.nodeRunTab', 'Run info')}</TabsTrigger>
    </TabsList>
    <TabsContent value="node"><NodeTab wfId={workflowId} readOnly selectedNodeId={nodeId} /></TabsContent>
    <TabsContent value="run-node">
    {nodeEvents ? <InspectorSection title={t('execution.nodeDetails')} icon={Activity}>
      {nodeEvents.length ? nodeEvents.map((event, index) => {
        const status = index === nodeEvents.length - 1 && executionStatus
          ? historicalNodeStatus(event.status, executionStatus) : event.status;
        const statusKey = status === 'success' ? 'succeeded' : status === 'error' ? 'failed' : status || 'running';
        return <div key={event.seq} className="mb-3 space-y-2 border-b border-edge-subtle pb-3 last:mb-0 last:border-0" data-role="readonly-node-event">
          <p className="text-xs text-content-secondary">{t(`execution.status.${statusKey}`, { defaultValue: statusKey })}</p>
          {event.inputs !== undefined && <NodeJsonPreview value={{ inputs: event.inputs }} />}
          {event.output !== undefined && <NodeJsonPreview value={{ output: event.output }} />}
          {event.error_message && <p role="alert" className="whitespace-pre-wrap break-words text-xs text-state-danger">{event.error_message}</p>}
        </div>;
      }) : <p className="text-sm text-content-secondary">{t('execution.nodeNotExecuted', 'This node has no execution record in this run.')}</p>}
    </InspectorSection> : <p className="text-sm text-content-secondary">{t('execution.snapshotNoRun', 'This is a workflow version snapshot with no attached execution.')}</p>}
    </TabsContent>
  </Tabs>;
}
