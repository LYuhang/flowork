import { downloadFilename, serializeWorkflow } from '@/lib/workflow/io';
import { workflowReference, type WorkflowFocus } from '@/lib/preview/workflow-reference';
import { PreviewReferenceButton } from './PreviewReferenceButton';
import { usePreviewOrigin } from '@/lib/preview/context-origin';
import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { applyEdgeChanges, applyNodeChanges, ReactFlowProvider, useNodesInitialized, useReactFlow, useStore, type Node } from '@xyflow/react';
import { Download, Maximize2, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import '@xyflow/react/dist/style.css';
import { workflowAtQuery } from '@/lib/api/queries/workflow';
import { AsyncState } from '@/components/ui/async-state';
import { Button } from '@/components/ui/button';
import { getBasePath } from '@/lib/base-path';
import { standaloneWorkflowPreviewHref } from '@/lib/preview/standalone-preview';
import { WorkflowGraph, workflowDictToNodesEdges } from '@/pages/canvas/WorkflowGraph';
import { autoLayout } from '@/pages/canvas/auto-layout';
import { WorkflowSnapshotContext } from '@/pages/canvas/WorkflowSnapshotContext';
import { ReadOnlyNodeDetails } from '@/pages/canvas/inspector/ReadOnlyNodeDetails';

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
function SnapshotCanvas({ graph, workflowId, version, focus, inspectorPlacement }: { focus?: WorkflowFocus | null; graph: Record<string, unknown>; workflowId: string; version: string; inspectorPlacement: 'bottom' | 'right' }) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState<string | null>(focus?.nodeIds[0] ?? null);
  const [inspectorOpen, setInspectorOpen] = useState(!!focus?.nodeIds.length);
  const projection = useMemo(() => {
    const projected = workflowDictToNodesEdges(graph);
    return { ...projected, nodes: autoLayout(projected.nodes, projected.edges) };
  }, [graph]);
  const [measuredNodes, setMeasuredNodes] = useState<Node[]>(() => projection.nodes.map(node => ({ ...node, selected: focus?.nodeIds.includes(node.id) ?? false })));
  const nodes = measuredNodes;
  const [edges, setEdges] = useState(() => projection.edges.map(edge => ({ ...edge, selected: edge.selectable !== false && !!focus?.edges.some(item => item.source === edge.source && item.target === edge.target) })));
  const [menu, setMenu] = useState<{ x: number; y: number; nodes: string[]; edges: { source: string; target: string }[] } | null>(null);
  const selectedNodes = nodes.filter(node => node.selected).map(node => node.id);
  const selectedEdges = edges.filter(edge => edge.selected && edge.selectable !== false);
  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') close(); };
    window.addEventListener('resize', close); window.addEventListener('scroll', close, true);
    document.addEventListener('keydown', key);
    return () => { window.removeEventListener('resize', close); window.removeEventListener('scroll', close, true); document.removeEventListener('keydown', key); };
  }, [menu]);
  const openMenu = (event: { preventDefault(): void; clientX: number; clientY: number }, nodeIds: string[], connections: {source: string; target: string}[]) => {
    event.preventDefault();
    setMenu({ x: Math.max(8, Math.min(event.clientX, window.innerWidth - 240)), y: Math.max(8, Math.min(event.clientY, window.innerHeight - 70)), nodes: nodeIds, edges: connections });
  };

  return (
    <WorkflowSnapshotContext.Provider value={graph}>
      <ReactFlowProvider>
        <SnapshotViewport />
        <div className={`flex min-h-0 flex-1 flex-col ${inspectorPlacement === 'right' ? 'md:flex-row' : ''}`}>
        <div className="relative min-h-0 flex-1">
          {nodes.length ? (
            <WorkflowGraph nodes={nodes} edges={edges}
              showMiniMap={false} showInteractiveControls={false}
              nodesDraggable={false} nodesConnectable={false} edgesReconnectable={false}
              edgesFocusable nodesFocusable elementsSelectable selectionOnDrag
              deleteKeyCode={null} selectionKeyCode="Shift" multiSelectionKeyCode={['Meta', 'Control']}
              onNodeContextMenu={(event, node) => openMenu(event, node.selected ? selectedNodes : [node.id], node.selected ? selectedEdges : [])}
              onEdgeContextMenu={(event, edge) => { if (edge.selectable !== false) openMenu(event, edge.selected ? selectedNodes : [], edge.selected ? selectedEdges : [edge]); }}
              onPaneContextMenu={event => openMenu(event, [], [])}
              onSelectionContextMenu={event => openMenu(event, selectedNodes, selectedEdges)}
              onEdgesChange={changes => setEdges(current => applyEdgeChanges(changes.filter(change => change.type === 'select'), current))}
              onNodeClick={(_, node) => { setSelected(node.id); if (inspectorPlacement === 'right') setInspectorOpen(true); }}
              onNodeDoubleClick={(_, node) => { setSelected(node.id); setInspectorOpen(true); }}
              onNodesChange={(changes) => {
                // Measurements are presentation state, not graph edits. Keep
                // them so xyflow can initialize and fit the resized viewport.
                const dimensions = changes.filter((change) => change.type === 'dimensions' || change.type === 'select');
                if (dimensions.length) setMeasuredNodes((current) => {
                  const measured = applyNodeChanges(dimensions, current);
                  return dimensions.some(change => change.type === 'dimensions')
                    ? autoLayout(measured, projection.edges) : measured;
                });
                const selection = changes.find((change) => change.type === 'select' && change.selected);
                if (selection?.type === 'select') setSelected(selection.id);
              }}
              onPaneClick={() => { setSelected(null); setInspectorOpen(false); setMenu(null); }}
            />
          ) : <div className="p-4 text-sm text-muted-foreground">{t('canvas.emptyReadOnly', 'This workflow version has no nodes.')}</div>}
          {selectedNodes.length + selectedEdges.length > 0 ? (
            <div className="absolute bottom-3 right-3 z-10 flex items-center gap-2 rounded-lg border bg-popover p-1 shadow-sm">
              <span className="px-2 text-xs">{t('preview.reference.selectionCount', '{{count}} selected', { count: selectedNodes.length + selectedEdges.length })}</span>
            </div>
          ) : null}
          {menu ? <div role="dialog" aria-label={t('composer.context.selectionActions', 'Selected text actions')}
            className="fixed z-50 flex items-center rounded-lg border bg-popover p-1 shadow-lg" style={{ left: menu.x, top: menu.y }}>
            <PreviewReferenceButton build={() => workflowReference(workflowId, version, workflowId, menu.nodes, menu.edges)} />
            <Button size="icon" variant="ghost" aria-label={t('close', 'Close')} onClick={() => setMenu(null)}><X className="h-4 w-4" /></Button>
          </div> : null}
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
            <div className="min-h-0 flex-1 overflow-auto p-3"><ReadOnlyNodeDetails workflowId={workflowId} nodeId={selected} /></div>
          </section>
        ) : null}
        </div>
      </ReactFlowProvider>
    </WorkflowSnapshotContext.Provider>
  );
}

/** Opening-time authorization is separate from the editor's cached draft. */
export function WorkflowPreviewRenderer({ workflowId, version, focus, allowOpenInNewPage = true, inspectorPlacement = 'bottom' }: { workflowId: string; version: string; focus?: WorkflowFocus | null; allowOpenInNewPage?: boolean; inspectorPlacement?: 'bottom' | 'right' }) {
  const { t } = useTranslation();
  const origin = usePreviewOrigin();
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
  const missingFocus = focus && (focus.nodeIds.some(id => !Object.hasOwn(graph, id)) || focus.edges.some(edge => {
    const node = graph[edge.source] as { children?: unknown } | undefined;
    return !Array.isArray(node?.children) || !node.children.includes(edge.target);
  }));
  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col" data-role="workflow-preview">
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-edge-subtle px-3 py-2 text-sm">
        <span className="min-w-0 flex-1 truncate">{String(query.data?.meta?.workflow_name || workflowId)}</span>
        <span className="shrink-0 text-muted-foreground">{version} · {t('preview.workflow.readOnly', 'Read only')}</span>

        <Button variant="ghost" size="icon" aria-label={t('io.download', 'Download JSON')}
          onClick={() => {
            const url = URL.createObjectURL(new Blob([serializeWorkflow(graph)], { type: 'application/json' }));
            const anchor = document.createElement('a');
            anchor.href = url; anchor.download = downloadFilename(String(query.data?.meta?.workflow_name || workflowId), version);
            document.body.appendChild(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(url);
          }}><Download className="h-4 w-4" /></Button>
        {allowOpenInNewPage ? (
          <Button asChild variant="ghost" size="icon">
            <a href={standaloneWorkflowPreviewHref(workflowId, version, origin)} target="_blank" rel="noopener noreferrer"
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
      {missingFocus ? <p role="alert" className="px-3 py-2 text-sm text-destructive">{t('preview.reference.missingObjects', 'Some referenced objects are unavailable in this version.')}</p> : null}
      <SnapshotCanvas key={workflowId + ':' + version + ':' + JSON.stringify(focus ?? null)} graph={graph} workflowId={workflowId} version={version} focus={focus} inspectorPlacement={inspectorPlacement} />
    </div>
  );
}
