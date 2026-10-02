import { useEffect, useMemo, useRef, useState } from 'react';
import { useParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { ReactFlowProvider, useNodes, useNodesInitialized, useNodesState, useReactFlow, type Node, type Edge } from '@xyflow/react';
import { Button } from '@/components/ui/button';
import { useExecutionDetail, useExecutionEvents, type ExecutionFrame } from '@/lib/api/queries/workflow-history';
import { WorkflowGraph, workflowDictToNodesEdges } from './WorkflowGraph';
import { WorkflowSnapshotContext } from './WorkflowSnapshotContext';
import { ExecutionHistoryContext } from './ExecutionHistoryContext';
import { NodeJsonPreview } from './nodes/NodeJsonPreview';
import { historicalNodeStatus } from './execution-state';
import { autoLayout } from './auto-layout';

export function ExecutionDetailPage() {
  const { executionId = '' } = useParams();
  return <ExecutionDetail key={executionId} executionId={executionId} />;
}

function ExecutionDetail({ executionId }: { executionId: string }) {
  const { t } = useTranslation();
  const detailQuery = useExecutionDetail(executionId);
  const eventsQuery = useExecutionEvents(executionId);
  const [selected, setSelected] = useState<string | null>(null);
  const detail = detailQuery.data;
  const projection = useMemo(() => {
    const graph = workflowDictToNodesEdges(detail?.workflow ?? null);
    return { ...graph, nodes: autoLayout(graph.nodes, graph.edges) };
  }, [detail?.workflow]);
  const latestNodeEvents = useMemo(() => {
    const nodes: Record<string, ExecutionFrame> = {};
    for (const event of eventsQuery.data?.events ?? []) {
      if (event.type === 'node_event' && event.node_id) nodes[event.node_id] = event;
    }
    return nodes;
  }, [eventsQuery.data]);
  if (detailQuery.isError || eventsQuery.isError) return <p role="alert" className="p-6">{t('execution.unavailable')}</p>;
  if (!detail) return <p role="status" className="p-6">{t('execution.loading')}</p>;
  const selectedEvents = eventsQuery.data?.events.filter((event) => event.type === 'node_event' && event.node_id === selected) ?? [];
  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-edge-structural px-4 py-3">
        <div className="min-w-0">
          <h1 className="text-base font-semibold">{t('execution.detail')} · {t(`execution.status.${detail.status}`)}</h1>
          <p className="text-xs text-content-secondary">{t('execution.readOnly')}</p>
          {detail.error_code && <p role="alert" className="text-sm text-state-danger">{t(`execution.failure.${detail.error_code}`, { defaultValue: t('execution.failure.generic') })}</p>}
          <p className="break-all font-mono text-xs text-content-tertiary">{detail.id}</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => { void detailQuery.refetch(); void eventsQuery.refetch(); }}>{t('execution.refresh')}</Button>
      </header>
      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <main className="min-h-80 min-w-0 flex-1">
          <WorkflowSnapshotContext.Provider value={detail.workflow}>
            <ExecutionHistoryContext.Provider value={{ detail, latestNodeEvents }}>
              <ReactFlowProvider>
                <ExecutionGraph initialNodes={projection.nodes} edges={projection.edges} onSelect={setSelected} />
              </ReactFlowProvider>
            </ExecutionHistoryContext.Provider>
          </WorkflowSnapshotContext.Provider>
        </main>
        <aside className="max-h-72 overflow-auto border-t border-edge-structural p-4 lg:max-h-none lg:w-80 lg:border-l lg:border-t-0">
          <h2 className="mb-3 text-sm font-medium">{t('execution.inputs')}</h2>
          <div className="mb-4"><NodeJsonPreview value={detail.inputs} /></div>
          <h2 className="mb-3 text-sm font-medium">{t('execution.nodeDetails')}{selected ? ` · ${selected}` : ''}</h2>
          {selected ? selectedEvents.map((event) => {
            const status = event.seq === latestNodeEvents[selected]?.seq ? historicalNodeStatus(event.status, detail.status) : event.status;
            return <div key={event.seq} className="mb-3 border-b border-edge-structural pb-3">
            <p className="mb-2 text-xs text-content-secondary">{t(`execution.status.${status === 'success' ? 'succeeded' : status === 'error' ? 'failed' : status === 'cancelled' ? 'cancelled' : 'running'}`)}</p>
            {event.inputs !== undefined && <NodeJsonPreview value={{ inputs: event.inputs }} />}
            {event.output !== undefined && <NodeJsonPreview value={{ output: event.output }} />}
            {event.error_message && <p role="alert" className="break-words text-xs text-state-danger">{event.error_message}</p>}
          </div>; })
            : <p className="text-sm text-content-secondary">{t('execution.noNodeSelected')}</p>}
        </aside>
      </div>
    </div>
  );
}

/** Presentation-only positions: the immutable execution snapshot is never edited. */
function ExecutionGraph({ initialNodes, edges, onSelect }: { initialNodes: Node[]; edges: Edge[]; onSelect: (id: string) => void }) {
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  return <WorkflowGraph nodes={nodes} edges={edges} onNodesChange={onNodesChange}
    nodesDraggable={false} nodesConnectable={false} edgesReconnectable={false}
    deleteKeyCode={null} showInteractiveControls={false}
    onNodeClick={(_event, node) => onSelect(node.id)}>
    <MeasuredLayout edges={edges} onLayout={setNodes} />
  </WorkflowGraph>;
}

function MeasuredLayout({ edges, onLayout }: { edges: Edge[]; onLayout: (nodes: Node[]) => void }) {
  const initialized = useNodesInitialized();
  const nodes = useNodes();
  const dimensions = nodes.map((node) => `${node.id}:${node.measured?.width}:${node.measured?.height}`).join('|');
  const { getNodes, fitView } = useReactFlow();
  const fitted = useRef(false);
  useEffect(() => {
    if (!initialized) return;
    onLayout(autoLayout(getNodes(), edges));
    // Fit once after measurement. Later output expansion must preserve the
    // user's zoom and viewport rather than snapping back on each update.
    if (!fitted.current) {
      const frame = requestAnimationFrame(() => {
        void fitView({ padding: 0.16, minZoom: 0.1, maxZoom: 1 });
        fitted.current = true;
      });
      return () => cancelAnimationFrame(frame);
    }
  }, [initialized, dimensions, edges, getNodes, fitView, onLayout]);
  return null;
}
