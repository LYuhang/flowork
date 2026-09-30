/**
 * Inspector → Node tab — selected-node editor.
 *
 * Selection is read live from xyflow via `useNodes()`; the first
 * `selected: true` node wins. Edits flow through the local store's
 * `applyEdit(mutator)` (see `stores/workflow-edit.ts`), which clones
 * the draft, pushes the previous snapshot onto the undo stack, and
 * flips `dirty` so the toolbar Save button enables.
 *
 * T8.5 wired the three lower sections: input fields, output fields, and
 * the per-type config editor. Text inputs across all sections commit on
 * blur (see `CommitOnBlur.tsx`) so the undo stack records one entry per
 * field change rather than one per keystroke — matches legacy UX.
 *
 * Strict-TS notes:
 *   - `useNodes()` returns `Node<Record<string, unknown>>[]`. We
 *     narrow `data` to a local `NodePayload` view so we can read
 *     name/description/fields without `any`.
 *   - Inside `applyEdit`, the draft entry is `unknown` until we cast
 *     to `Record<string, unknown>` and assign the field — the mutator
 *     returns the draft as required by the store's contract.
 */
import { useEffect } from 'react';
import { useNodes } from '@xyflow/react';
import { useTranslation } from 'react-i18next';
import { Info } from 'lucide-react';
import { Label } from '@/components/ui/label';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import {
  CommitOnBlurInput,
  CommitOnBlurTextarea,
} from '@/pages/canvas/inspector/CommitOnBlur';
import {
  FieldsEditor,
  type FieldsMap,
} from '@/pages/canvas/inspector/FieldsEditor';
import { NodeConfigEditor } from '@/pages/canvas/inspector/NodeConfigEditor';
import {
  mirrorOutputsFromInputs,
  outputsFollowInputs,
} from '@/pages/canvas/nodes/outputsFollowInputs';
import {
  fixedOutputFields,
  outputsMatchFixed,
} from '@/pages/canvas/nodes/fixedOutputs';
import { hasReservedNodeName } from '@/lib/workflow/node-type-defaults';
import { nodeTypeHasConfig } from '@/pages/canvas/inspector/node-config-registry';
import { computeReferenceCandidates } from './node-reference-candidates';
import { InspectorSection } from './InspectorSection';
import { useWorkflowSnapshot } from '@/pages/canvas/WorkflowSnapshotContext';

interface NodePayload {
  node_id?: string;
  node_name?: string;
  node_description?: string;
  node_type?: string;
  input_fields?: FieldsMap;
  output_fields?: FieldsMap;
  node_config?: Record<string, unknown>;
}

/**
 * Reference dropdown candidates for the selected node's INPUT fields: every
 * `producer_node_name.output_field` whose producer is an ANCESTOR (predecessor)
 * of the selected node in the DAG. A node's input can only come from the output
 * of nodes that run BEFORE it — so we restrict the list to the upstream set
 * (`ancestorsOf`), which naturally excludes the node itself AND all of its
 * descendants/children. Computed from the draft (the source of truth), not the
 * xyflow projection — so the list reflects in-flight edits.
 */
export interface NodeTabProps {
  wfId: string;
  /**
   * When true (T14: pinned historical version), the name + description
   * inputs are disabled and the field-/config-editor subtrees receive
   * `readOnly` so every nested input renders disabled.
   */
  readOnly?: boolean;
}

export function NodeTab({ wfId, readOnly = false }: NodeTabProps) {
  const { t } = useTranslation();
  const snapshot = useWorkflowSnapshot();
  const nodes = useNodes();
  const selected = nodes.find((n) => n.selected);

  if (!selected) {
    return (
      <p className="text-sm text-muted-foreground">
        {t('inspector.node.select', 'Select a node to edit.')}
      </p>
    );
  }

  // `key={selected.id}` remounts the editor subtree on a selected-node change
  // so no transient editor state leaks across undo / agent / version-switch
  // (Stream 0b). `selected.data` flows in as the live payload projection.
  return (
    <NodeTabEditor
      key={selected.id}
      wfId={wfId}
      nodeId={selected.id}
      payload={(selected.data ?? {}) as NodePayload}
      readOnly={readOnly || !!snapshot}
    />
  );
}

interface NodeTabEditorProps {
  wfId: string;
  nodeId: string;
  payload: NodePayload;
  readOnly: boolean;
}

function NodeTabEditor({ wfId, nodeId, payload, readOnly }: NodeTabEditorProps) {
  const { t } = useTranslation();
  const snapshot = useWorkflowSnapshot();
  const applyEdit = useWorkflowEditStore((s) => s.applyEdit);
  const draft = useWorkflowEditStore((s) => snapshot ?? s.draft);

  const nodeType = payload.node_type ?? 'UnknownNode';
  const mirrors = outputsFollowInputs(nodeType);
  // Start/End names are engine-reserved constants ('__start__' / '__end__');
  // editing them into anything else fails Workflow.check, so the field is
  // read-only for those types.
  const nameReserved = hasReservedNodeName(nodeType);
  // Reserved-name nodes (Start/End) carry no meaningful, user-authorable
  // description — the canned "start of the workflow" / "end of your workflow"
  // sub-text is pure noise. Suppress the whole Description block for them to
  // keep the header clean (Change 1). All other types keep the field.
  const showDescription = !nameReserved;
  // Config-less types (Start/End/LoopEnd/unknown) render no Config section + no
  // preceding separator (Change 3).
  const showConfig = nodeTypeHasConfig(nodeType);
  // Control-flow nodes carry a partial-or-empty data interface; gate the input
  // and output blocks independently (a node may show one but not the other):
  //   - Parallel start/end: NO input + NO output (purely functional).
  //   - LoopEndNode: NO input + NO output (mirrors Parallel; loop results live
  //     on the paired LoopBegin's `loop_output`, not here).
  //   - LoopBeginNode: NO authorable input; output IS shown but READ-ONLY —
  //     the engine fixes its outputs to { i, loop_output } and the user does
  //     not author them.
  const isParallel =
    nodeType === 'ParallelStartNode' || nodeType === 'ParallelEndNode';
  const isLoopBegin = nodeType === 'LoopBeginNode';
  const isLoopEnd = nodeType === 'LoopEndNode';
  // Engine-FIXED output schema (HTTPRequest/LoopBegin/Condition/
  // Template/TableRead/TableWrite). When set we PRESET these output fields +
  // render them read-only; the engine enforces the exact set (Node.check), so
  // the user neither authors nor edits them. See `fixedOutputs.ts`.
  const isSubAgent = nodeType === 'SubAgentNode';
  const traceField = { type: 'array', description: 'Runtime execution messages (ChatML)' };
  const fixedOutputs = fixedOutputFields(nodeType);
  const hasFixedOutputs = fixedOutputs !== undefined;
  const showInputFields = !isParallel && !isLoopBegin && !isLoopEnd;
  const showOutputFields = !isParallel && !isLoopEnd;
  // Fixed-output and LoopBegin outputs are shown but render read-only.
  const outputFieldsReadOnly = readOnly || hasFixedOutputs;
  // The whole fields block (+ its preceding separator) only renders if at least
  // one of the two sub-blocks is visible — no dangling separator otherwise.
  const showFields = showInputFields || showOutputFields;
  const nameId = `node-name-${nodeId}`;
  const descId = `node-description-${nodeId}`;

  const inputFields = payload.input_fields ?? {};
  const referenceCandidates = computeReferenceCandidates(draft, nodeId);

  const editNode = (mutate: (entry: Record<string, unknown>) => void) => {
    if (readOnly) return;
    applyEdit((wf) => {
      const entry = wf[nodeId];
      if (entry && typeof entry === 'object') mutate(entry as Record<string, unknown>);
      return wf;
    });
  };

  const onNameCommit = (value: string) =>
    editNode((entry) => {
      entry.node_name = value;
    });

  const onDescriptionCommit = (value: string) =>
    editNode((entry) => {
      entry.node_description = value;
    });

  const onInputFieldsChange = (next: FieldsMap) =>
    editNode((entry) => {
      entry.input_fields = next;
      // Output-follows-input: materialize the mirror in the SAME applyEdit so
      // the persisted output_fields stays faithful (one undo step). We chose
      // edit-time materialization over Save-time because it keeps the whole
      // mirror concern inside this stream's files (no Save-path coupling).
      if (outputsFollowInputs(entry.node_type as string | undefined)) {
        entry.output_fields = mirrorOutputsFromInputs(next);
      }
    });

  const onOutputFieldsChange = (next: FieldsMap) =>
    editNode((entry) => {
      entry.output_fields = isSubAgent ? { ...next, __traces__: traceField } : next;
    });

  const onConfigChange = (next: Record<string, unknown>) =>
    editNode((entry) => {
      entry.node_config = next;
    });

  // Output VIEW:
  //   - fixed-output node → the engine-fixed preset (constant, overrides
  //     whatever the stored node carries; the backfill effect below also
  //     persists it into the draft so the route Check passes).
  //   - mirroring node → derived from the current inputs (the draft also
  //     carries the materialized copy, but deriving here keeps the view in
  //     lockstep even before the next input edit lands).
  //   - otherwise → the stored output_fields.
  const outputFields = snapshot ? payload.output_fields ?? {} : fixedOutputs
    ? fixedOutputs
    : mirrors
      ? mirrorOutputsFromInputs(inputFields)
      : payload.output_fields ?? {};

  // Backfill the draft so a fixed-output node's persisted `output_fields`
  // MATCHES the engine-enforced preset (Node.check requires the EXACT set, and
  // downstream references resolve against these names). We do this on open /
  // when the stored set drifts — one `applyEdit` (so it's a single undo step,
  // committed on Save). Skipped in read-only (pinned historical) mode so we
  // never mutate a viewed-only version. The `outputsMatchFixed` guard keeps
  // this a no-op once the draft already carries the preset (no edit loop).
  useEffect(() => {
    if (readOnly) return;
    if (isSubAgent && payload.output_fields?.__traces__?.type !== 'array') {
      editNode((entry) => {
        entry.output_fields = { ...(entry.output_fields as FieldsMap ?? {}), __traces__: traceField };
      });
    }
    if (!fixedOutputs) return;
    if (outputsMatchFixed(payload.output_fields, fixedOutputs)) return;
    editNode((entry) => {
      entry.output_fields = structuredClone(fixedOutputs);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId, nodeType, readOnly]);

  // Three visually-separated blocks (divided by a Separator, i.e. "---") so a
  // non-technical user can tell them apart at a glance:
  //   1. identity — name + description
  //   2. data interface — input + output field definitions
  //   3. behavior — per-type config
  return (
    <div className="space-y-5 pb-4">
      {/* Block 1 — identity */}
      <InspectorSection
        title={t('inspector.node.details', 'Node details')}
        icon={Info}
        testId="inspector-section-details"
        actions={(
          <span className="rounded-md border border-edge-subtle bg-background px-1.5 py-0.5 font-mono text-xs text-content-tertiary">
            {nodeType}
          </span>
        )}
      >
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label
              htmlFor={nameId}
              className="block text-[13px] font-medium text-content-secondary"
            >
              {t('inspector.node.name', 'Name')}
            </Label>
            <CommitOnBlurInput
              id={nameId}
              value={payload.node_name ?? ''}
              onCommit={onNameCommit}
              placeholder={t('inspector.node.name_placeholder', 'Node name')}
              disabled={readOnly || nameReserved}
              data-name-reserved={nameReserved ? 'true' : undefined}
            />
          </div>

          {showDescription && (
            <div className="space-y-1.5">
              <Label
                htmlFor={descId}
                className="block text-[13px] font-medium text-content-secondary"
              >
                {t('inspector.node.description', 'Description')}
              </Label>
              <CommitOnBlurTextarea
                id={descId}
                value={payload.node_description ?? ''}
                onCommit={onDescriptionCommit}
                placeholder={t(
                  'inspector.node.description_placeholder',
                  'Describe what this node does',
                )}
                rows={3}
                disabled={readOnly}
              />
            </div>
          )}
        </div>
      </InspectorSection>

      {/* Block 2 — input / output field definitions. Each sub-block is gated
          independently: Parallel start/end + LoopEnd show neither; LoopBegin
          hides INPUT but shows OUTPUT read-only (engine-fixed { i, loop_output }).
          The Separator only renders when at least one sub-block is visible, so
          there is never a dangling separator. */}
      {showFields && (
        <div className="space-y-5">
          {showInputFields && (
            <FieldsEditor
              title={t('inspector.node.input_fields', 'Input fields')}
              mode="input"
              fields={inputFields}
              referenceCandidates={referenceCandidates}
              readOnly={readOnly}
              onChange={onInputFieldsChange}
            />
          )}

          {isSubAgent && (
            <FieldsEditor
              title={t('inspector.node.execution_traces', 'Execution traces')}
              mode="output"
              fields={{ __traces__: traceField }}
              readOnly
              onChange={() => {}}
            />
          )}
          {showOutputFields && (
            <FieldsEditor
              title={t('inspector.node.output_fields', 'Output fields')}
              mode="output"
              fields={isSubAgent ? Object.fromEntries(Object.entries(outputFields).filter(([name]) => name !== '__traces__')) : outputFields}
              outputsFollowInputs={mirrors}
              readOnly={outputFieldsReadOnly}
              onChange={onOutputFieldsChange}
            />
          )}
        </div>
      )}

      {/* Block 3 — per-type config (hidden for config-less types: no empty
          section, no dangling separator). */}
      {showConfig && (
        <NodeConfigEditor
          nodeType={nodeType}
          config={payload.node_config ?? {}}
          inputFieldNames={Object.keys(inputFields)}
          inputFields={inputFields}
          outputFieldNames={Object.keys(outputFields)}
          nodeId={nodeId}
          wfId={wfId}
          readOnly={readOnly}
          onChange={onConfigChange}
        />
      )}
    </div>
  );
}
