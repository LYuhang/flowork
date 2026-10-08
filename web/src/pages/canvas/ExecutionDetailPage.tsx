import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { ReactFlowProvider, useNodes, useNodesInitialized, useNodesState, useReactFlow, type Node, type Edge } from '@xyflow/react';
import { Button } from '@/components/ui/button';
import { useExecutionDetail, useExecutionEvents, type ExecutionFrame } from '@/lib/api/queries/workflow-history';
import { WorkflowGraph, workflowDictToNodesEdges } from './WorkflowGraph';
import { WorkflowSnapshotContext } from './WorkflowSnapshotContext';
import { ExecutionHistoryContext } from './ExecutionHistoryContext';
import { NodeJsonPreview } from './nodes/NodeJsonPreview';
import { ReadOnlyNodeDetails } from './inspector/ReadOnlyNodeDetails';
import { autoLayout } from './auto-layout';

export function ExecutionDetailPage() {
  const { executionId = '' } = useParams();
  return <ExecutionDetail key={executionId} executionId={executionId} />;
}

function ExecutionDetail({ executionId }: { executionId: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const detailQuery = useExecutionDetail(executionId);
  const eventsQuery = useExecutionEvents(executionId);
  const [selected, setSelected] = useState<string | null>(null);
  const detail = detailQuery.data;
  const errors = detail?.result?.error_dict;
  const errorEntries = errors && typeof errors === 'object' && !Array.isArray(errors)
    ? Object.entries(errors).filter((entry): entry is [string, string] => typeof entry[1] === 'string' && entry[1].length > 0) : [];
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
  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-edge-structural px-4 py-3">
        <div className="min-w-0">
          <h1 className="text-base font-semibold">{t('execution.detail')} · {t(`execution.status.${detail.status}`)}</h1>
          <p className="text-xs text-content-secondary">{t('execution.readOnly')}</p>
          {detail.error_code && <p role="alert" className="text-sm text-state-danger">{t(`execution.failure.${detail.error_code}`, { defaultValue: t('execution.failure.generic') })}</p>}
          <p className="break-all font-mono text-xs text-content-tertiary">{detail.id}</p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => navigate(detail.source_type === 'task' ? `/tasks/${encodeURIComponent(detail.source_id)}` : detail.source_type === 'deployment' ? `/deployments/${encodeURIComponent(detail.source_id)}` : `/workflow/${encodeURIComponent(detail.wf_id)}`)}>{t('preview.standalone.back', 'Back')}</Button>
          <Button variant="outline" size="sm" onClick={() => { void detailQuery.refetch(); void eventsQuery.refetch(); }}>{t('execution.refresh')}</Button>
        </div>
      </header>
      <WorkflowSnapshotContext.Provider value={detail.workflow}>
      <ExecutionHistoryContext.Provider value={{ detail, latestNodeEvents }}>
      <ReactFlowProvider>
      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <main className="min-h-80 min-w-0 flex-1">
          <ExecutionGraph initialNodes={projection.nodes} edges={projection.edges} onSelect={setSelected} />
        </main>
        <aside data-role="execution-inspector" className="max-h-72 overflow-auto border-t border-edge-structural p-4 lg:max-h-none lg:w-[420px] lg:max-w-[45%] lg:shrink-0 lg:border-l lg:border-t-0">
          {errorEntries.length > 0 && <section aria-label={t('execution.errors')} className="mb-4">
            <h2 className="mb-3 text-sm font-medium">{t('execution.errors')}</h2>
            {errorEntries.map(([node, message]) => <div key={node} className="mb-2 rounded border border-state-danger/30 p-3 text-sm">
              <p className="mb-1 font-mono text-xs text-content-secondary">{node}</p>
              <p className="whitespace-pre-wrap break-words text-state-danger">{message}</p>
            </div>)}
          </section>}
          <h2 className="mb-3 text-sm font-medium">{t('execution.inputs')}</h2>
          <div className="mb-4"><NodeJsonPreview value={detail.inputs} /></div>
          <h2 className="mb-3 text-sm font-medium">{t('preview.workflow.nodeDetails')}{selected ? ` · ${selected}` : ''}</h2>
          {selected ? <ReadOnlyNodeDetails workflowId={detail.wf_id} nodeId={selected}
            events={eventsQuery.data?.events ?? []} executionStatus={detail.status} />
            : <p className="text-sm text-content-secondary">{t('execution.noNodeSelected')}</p>}
        </aside>
      </div>
      </ReactFlowProvider>
      </ExecutionHistoryContext.Provider>
      </WorkflowSnapshotContext.Provider>
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
