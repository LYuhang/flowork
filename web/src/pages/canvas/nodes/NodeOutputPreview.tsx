import { memo, useId, useMemo, useState } from 'react';
import { useParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useRunNodeResult, useVfsRunList } from '@/lib/api/queries/vfs';
import { useExecStreamStore } from '@/stores/exec-stream';
import { RenderedPreview } from './RenderedPreview';
import { parseRenderedResult } from './template-preview-media';
import { NodeJsonPreview } from './NodeJsonPreview';

interface NodeOutputPreviewProps {
  nodeId: string;
  nodeType: string;
  format?: string;
}

/** All node types share output discovery; only Template opts into rich rendering. */
export const NodeOutputPreview = memo(function NodeOutputPreview(props: NodeOutputPreviewProps) {
  const { wfId } = useParams<{ wfId: string }>();
  return wfId ? <WorkflowNodeOutputPreview key={`${wfId}:${props.nodeId}`} {...props} wfId={wfId} /> : null;
});

function WorkflowNodeOutputPreview({ nodeId, nodeType, format, wfId }: NodeOutputPreviewProps & { wfId: string }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(true);
  const previewId = useId();
  const nodeStatus = useExecStreamStore((s) => s.wfId === wfId ? s.perNode[nodeId]?.status : undefined);
  const liveResult = useExecStreamStore((s) => s.wfId === wfId ? s.perNode[nodeId]?.result : undefined);
  const resultPath = `/run/__exec__/nodes/${nodeId}.json`;
  // One shared directory request discovers which nodes really have persisted
  // output.  Without this guard every canvas node issued its own guaranteed
  // 404 before the first run, making large workflows feel frozen.
  const runFiles = useVfsRunList(wfId);
  const hasPersistedResult = runFiles.data?.entries.some(
    (entry) => entry.path === resultPath,
  ) ?? false;
  const completedLiveResult = nodeStatus === 'completed' ? liveResult : undefined;
  const persisted = useRunNodeResult(
    completedLiveResult === undefined && hasPersistedResult ? wfId : null,
    nodeId,
  );
  const savedOutput = persisted?.output;
  const output = useMemo(() => {
    if (completedLiveResult === undefined) return savedOutput;
    try { return JSON.parse(completedLiveResult) as unknown; } catch { return completedLiveResult; }
  }, [completedLiveResult, savedOutput]);
  const parsed = useMemo(() => nodeType === 'TemplateNode'
    ? parseRenderedResult(typeof output === 'string' ? output : JSON.stringify(output))
    : null, [nodeType, output]);
  // Explicit null, false, 0, empty strings/objects/arrays are real outputs.
  if (output === undefined) return null;

  return (
    <div
      className="nodrag nopan nowheel w-56 rounded-b-md border border-t-0 border-edge-structural bg-surface-raised"
      data-node-output-preview={nodeId}
      data-template-node-preview={nodeType === 'TemplateNode' ? nodeId : undefined}
      onDoubleClick={(event) => event.stopPropagation()}
    >
      <button
        type="button"
        className="flex w-full items-center gap-1.5 px-2.5 py-2 text-left text-xs font-medium text-content-secondary hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus"
        aria-expanded={expanded}
        aria-controls={previewId}
        onClick={(event) => { event.stopPropagation(); setExpanded((value) => !value); }}
      >
        {expanded ? <ChevronDown className="h-3.5 w-3.5" aria-hidden /> : <ChevronRight className="h-3.5 w-3.5" aria-hidden />}
        {t('canvas.outputPreview.title', 'Output preview')}
      </button>
      {expanded ? (
        <div id={previewId} className="max-h-60 overflow-auto px-2.5 pb-2.5 [&_iframe]:h-52 [&_[data-testid=rendered-preview]]:max-h-none" onClick={(event) => event.stopPropagation()}>
          {parsed ? (
            <RenderedPreview rendered={parsed.rendered} format={parsed.format || format || 'text'} wfId={wfId} runId={wfId} />
          ) : <NodeJsonPreview value={output} />}
        </div>
      ) : null}
    </div>
  );
}
