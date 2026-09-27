import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { applyNodeChanges, ReactFlowProvider, useNodesInitialized, useReactFlow, useStore } from '@xyflow/react';
import { Maximize2, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import '@xyflow/react/dist/style.css';
import { workflowAtQuery } from '@/lib/api/queries/workflow';
import { AsyncState } from '@/components/ui/async-state';
import { Button } from '@/components/ui/button';
import { getBasePath } from '@/lib/base-path';
import { standaloneWorkflowPreviewHref } from '@/lib/preview/standalone-preview';
import { WorkflowGraph, workflowDictToNodesEdges } from '@/pages/canvas/WorkflowGraph';
import { WorkflowSnapshotContext } from '@/pages/canvas/WorkflowSnapshotContext';
import { NodeTab } from '@/pages/canvas/inspector/NodeTab';

/** Keep the whole graph visible when the Inspector changes the canvas area. */
function SnapshotViewport() {
  const width = useStore((state) => state.width);
  const height = useStore((state) => state.height);
  const initialized = useNodesInitialized();
  const { fitView } = useReactFlow();
  useEffect(() => {
    if (!initialized || !width || !height) return;
    const frame = requestAnimationFrame(() => { void fitView({ padding: 0.16, minZoom: 0.1, maxZoom: 1 }); });
    return () => cancelAnimationFrame(frame);
  }, [width, height, initialized, fitView]);
  return null;
}

/** Same canvas and Inspector as the editor, with a private selection/snapshot. */
function SnapshotCanvas({ graph, workflowId, inspectorPlacement }: { graph: Record<string, unknown>; workflowId: string; inspectorPlacement: 'bottom' | 'right' }) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState<string | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const projection = useMemo(() => workflowDictToNodesEdges(graph), [graph]);
  const [measuredNodes, setMeasuredNodes] = useState(projection.nodes);
  const nodes = useMemo(() => measuredNodes.map((node) => ({ ...node, selected: node.id === selected })), [measuredNodes, selected]);

  return (
    <WorkflowSnapshotContext.Provider value={graph}>
      <ReactFlowProvider>
        <SnapshotViewport />
        <div className={`flex min-h-0 flex-1 flex-col ${inspectorPlacement === 'right' ? 'md:flex-row' : ''}`}>
        <div className="relative min-h-0 flex-1">
          {nodes.length ? (
            <WorkflowGraph nodes={nodes} edges={projection.edges}
              showMiniMap={false} showInteractiveControls={false}
              nodesDraggable={false} nodesConnectable={false} edgesReconnectable={false}
              edgesFocusable={false} deleteKeyCode={null} selectionKeyCode={null} multiSelectionKeyCode={null}
              onNodeClick={(_, node) => { setSelected(node.id); if (inspectorPlacement === 'right') setInspectorOpen(true); }}
              onNodeDoubleClick={(_, node) => { setSelected(node.id); setInspectorOpen(true); }}
              onNodesChange={(changes) => {
                // Measurements are presentation state, not graph edits. Keep
                // them so xyflow can initialize and fit the resized viewport.
                const dimensions = changes.filter((change) => change.type === 'dimensions');
                if (dimensions.length) setMeasuredNodes((current) => applyNodeChanges(dimensions, current));
                const selection = changes.find((change) => change.type === 'select' && change.selected);
                if (selection?.type === 'select') setSelected(selection.id);
              }}
              onPaneClick={() => { setSelected(null); setInspectorOpen(false); }}
            />
          ) : <div className="p-4 text-sm text-muted-foreground">{t('canvas.emptyReadOnly', 'This workflow version has no nodes.')}</div>}
          {selected && !inspectorOpen ? (
            <Button variant="outline" size="sm" className="absolute right-3 top-3" onClick={() => setInspectorOpen(true)}>
              {t('preview.workflow.nodeDetails', 'Node details')}
            </Button>
          ) : null}
        </div>
        {inspectorOpen && selected ? (
          <section className={`flex h-[48%] min-h-0 shrink-0 flex-col border-t border-edge-structural bg-surface-raised ${inspectorPlacement === 'right' ? 'md:h-full md:w-[420px] md:max-w-[45%] md:border-l md:border-t-0' : ''}`} aria-label={t('preview.workflow.nodeDetails', 'Node details')}>
            <div className="flex shrink-0 items-center gap-2 border-b border-edge-subtle px-3 py-1.5 text-sm">
              <span className="min-w-0 flex-1 truncate font-medium">{t('inspector.title', 'Inspector')} · {selected}</span>
              <span className="text-xs text-muted-foreground">{t('preview.workflow.readOnly', 'Read only')}</span>
              <Button variant="ghost" size="icon" aria-label={t('close', 'Close')} onClick={() => setInspectorOpen(false)}><X className="h-4 w-4" /></Button>
            </div>
            <div className="min-h-0 flex-1 overflow-auto p-3"><NodeTab wfId={workflowId} readOnly /></div>
          </section>
        ) : null}
        </div>
      </ReactFlowProvider>
    </WorkflowSnapshotContext.Provider>
  );
}

/** Opening-time authorization is separate from the editor's cached draft. */
export function WorkflowPreviewRenderer({ workflowId, version, allowOpenInNewPage = true, inspectorPlacement = 'bottom' }: { workflowId: string; version: string; allowOpenInNewPage?: boolean; inspectorPlacement?: 'bottom' | 'right' }) {
  const { t } = useTranslation();
  const match = /^v([1-9]\d*)\.sv(\d+)$/.exec(version);
  const query = useQuery({
    ...workflowAtQuery(workflowId, Number(match?.[1]), Number(match?.[2])),
    queryKey: ['workflow-preview', workflowId, version],
    enabled: !!workflowId && !!match,
    staleTime: 0,
    gcTime: 0,
    refetchOnMount: 'always',
    retry: false,
  });
  const graph = query.data?.workflow as Record<string, unknown> | undefined;
  if (!match || !workflowId) return <AsyncState kind="error" title={t('preview.workflow.invalid', 'Invalid workflow preview reference.')} />;
  // Never expose private cached nodes while authorization is pending or failed.
  if (query.isFetching || query.isPending) return <AsyncState kind="loading" title={t('chat.preview.loadingWorkflow', 'Loading workflow...')} />;
  if (query.isError || !graph) return (
    <div className="flex h-full flex-col items-center justify-center gap-3 p-4">
      <p role="alert" className="text-sm text-destructive">{t('preview.workflow.unavailable', 'This workflow version is unavailable or you no longer have access.')}</p>
      <Button variant="outline" onClick={() => void query.refetch()}>{t('retry', 'Retry')}</Button>
    </div>
  );
  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col" data-role="workflow-preview">
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-edge-subtle px-3 py-2 text-sm">
        <span className="min-w-0 flex-1 truncate">{String(query.data?.meta?.workflow_name || workflowId)}</span>
        <span className="shrink-0 text-muted-foreground">{version} · {t('preview.workflow.readOnly', 'Read only')}</span>
        {allowOpenInNewPage ? (
          <Button asChild variant="ghost" size="icon">
            <a href={standaloneWorkflowPreviewHref(workflowId, version)} target="_blank" rel="noopener noreferrer"
              aria-label={t('tool.interactive.open_preview_tab', 'Open in a new Preview tab')} title={t('tool.interactive.open_preview_tab', 'Open in a new Preview tab')}>
              <Maximize2 className="h-4 w-4" />
            </a>
          </Button>
        ) : null}
        <a className="shrink-0 rounded text-sm underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          href={getBasePath() + '/workflow/' + encodeURIComponent(workflowId)} target="_blank" rel="noopener noreferrer">
          {t('preview.workflow.openLatest', 'Open latest canvas')}
        </a>
      </div>
      <SnapshotCanvas key={workflowId + ':' + version} graph={graph} workflowId={workflowId} inspectorPlacement={inspectorPlacement} />
    </div>
  );
}
