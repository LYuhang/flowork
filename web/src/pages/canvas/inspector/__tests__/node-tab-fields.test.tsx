/**
 * Stream 5 — NodeTab field wiring: reference candidates, output-follows-input
 * materialization, and no-local-state-leak across selected.id (remount).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

vi.mock('@/lib/api/queries/enums', () => ({
  useEnums: () => ({ data: { field_types: ['string', 'number', 'boolean'] } }),
  getEnumList: (enums: Record<string, unknown> | undefined, key: string) =>
    Array.isArray(enums?.[key]) ? (enums![key] as string[]) : [],
}));

// Seed a controllable xyflow selection; the projected `data` is the node payload.
const selection = { nodes: [] as { id: string; selected: boolean; data: unknown }[] };
vi.mock('@xyflow/react', () => ({
  useNodes: () => selection.nodes,
}));

// Avoid the per-type config editor pulling in heavy deps / queries. Keep the
// REAL `nodeTypeHasConfig` (a pure type→bool lookup) so NodeTab's config-gating
// (Change 3) is exercised; only stub the visual editor component.
vi.mock('@/pages/canvas/inspector/NodeConfigEditor', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('@/pages/canvas/inspector/NodeConfigEditor')>();
  return {
    ...actual,
    NodeConfigEditor: () => null,
  };
});

import { NodeTab } from '@/pages/canvas/inspector/NodeTab';
import { computeReferenceCandidates } from '@/pages/canvas/inspector/node-reference-candidates';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import { WorkflowSnapshotContext } from '@/pages/canvas/WorkflowSnapshotContext';

function seed(draft: Record<string, unknown>, selectedId: string) {
  useWorkflowEditStore.getState().setDraft(draft);
  const node = draft[selectedId] as Record<string, unknown>;
  selection.nodes = [{ id: selectedId, selected: true, data: node }];
}

describe('computeReferenceCandidates', () => {
  it('lists ONLY ancestor producers (predecessors), never descendants', () => {
    // start → fetch.  fetch's candidates = {start}; start's = {} (no ancestors).
    const draft = {
      node_1: {
        node_name: 'start',
        output_fields: { q: { type: 'string' } },
        children: ['node_2'],
      },
      node_2: {
        node_name: 'fetch',
        output_fields: { rows: { type: 'array' } },
        children: [],
      },
      __meta__: {},
    };
    expect(computeReferenceCandidates(draft, 'node_2')).toEqual(['start.q']);
    // start has no predecessor → it cannot reference its descendant `fetch`.
    expect(computeReferenceCandidates(draft, 'node_1')).toEqual([]);
  });
});

describe('NodeTab — isolated read-only snapshot', () => {
  it('shows historical output fields without backfilling or mutating the live editor', () => {
    const live = { node_1: { node_name: 'Live editor', node_type: 'StartNode', output_fields: {} } };
    useWorkflowEditStore.getState().setDraft(live);
    const before = useWorkflowEditStore.getState();
    const snapshot = { node_1: {
      node_name: 'Historical start', node_type: 'StartNode',
      input_fields: { currentInput: { type: 'string' } },
      output_fields: { historicalOutput: { type: 'number' } },
    } };
    selection.nodes = [{ id: 'node_1', selected: true, data: snapshot.node_1 }];
    render(<WorkflowSnapshotContext.Provider value={snapshot}><NodeTab wfId="wf" /></WorkflowSnapshotContext.Provider>);
    expect(screen.getByDisplayValue('Historical start')).toBeDisabled();
    expect(screen.getByTestId('field-mirror-name-historicalOutput')).toBeInTheDocument();
    expect(screen.queryByTestId('field-mirror-name-currentInput')).not.toBeInTheDocument();
    expect(screen.getByTestId('add-field-input')).toBeDisabled();
    expect(useWorkflowEditStore.getState().draft).toBe(before.draft);
    expect(useWorkflowEditStore.getState().undoStack).toBe(before.undoStack);
  });
});

describe('NodeTab — output follows input (StartNode)', () => {
  beforeEach(() => {
    useWorkflowEditStore.setState({
      draft: null,
      baseline: 'null',
      undoStack: [],
      redoStack: [],
    });
  });

  it('renders the output section as a read-only mirror', () => {
    seed(
      {
        node_1: {
          node_id: 'node_1',
          node_type: 'StartNode',
          input_fields: { q: { type: 'string', value: '', reference: '' } },
          output_fields: {},
        },
      },
      'node_1',
    );
    render(<NodeTab wfId="wf_1" />);
    expect(screen.getByTestId('outputs-mirror-caption')).toBeInTheDocument();
    expect(screen.getByTestId('field-mirror-name-q')).toHaveTextContent('q');
    expect(screen.queryByTestId('add-field-output')).toBeNull();
  });

  it('materializes output_fields into the draft when inputs change', async () => {
    seed(
      {
        node_1: {
          node_id: 'node_1',
          node_type: 'StartNode',
          input_fields: { q: { type: 'string', value: '', reference: '' } },
          output_fields: {},
        },
      },
      'node_1',
    );
    render(<NodeTab wfId="wf_1" />);
    // Add an input field → the same applyEdit must materialize the mirror.
    // addEntry names the new field `field_{names.length+1}` ⇒ `field_2` here.
    await userEvent.click(screen.getByTestId('add-field-input'));
    const draft = useWorkflowEditStore.getState().draft as Record<string, unknown>;
    const node = draft.node_1 as Record<string, unknown>;
    expect(Object.keys(node.input_fields as object)).toEqual(['q', 'field_2']);
    // output_fields mirrors name + type, no value/reference.
    expect(node.output_fields).toEqual({
      q: { type: 'string' },
      field_2: { type: 'string' },
    });
  });
});

describe('NodeTab — no local-state leak across selected.id change', () => {
  beforeEach(() => {
    useWorkflowEditStore.setState({ draft: null, baseline: 'null', undoStack: [], redoStack: [] });
  });

  it('a rejected-rename inline error does NOT survive a node switch (remount)', async () => {
    const draft = {
      node_1: {
        node_id: 'node_1',
        node_type: 'CodeNode',
        input_fields: { a: { type: 'string' }, b: { type: 'string' } },
        output_fields: {},
      },
      node_2: {
        node_id: 'node_2',
        node_type: 'CodeNode',
        input_fields: { z: { type: 'string' } },
        output_fields: {},
      },
    };
    seed(draft, 'node_1');
    const { rerender } = render(<NodeTab wfId="wf_1" />);
    // Trigger a duplicate-rename error on node_1's field.
    const nameInput = screen.getByTestId('field-name-a');
    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, 'b');
    await userEvent.tab();
    expect(screen.getByText(/already used/i)).toBeInTheDocument();

    // Switch selection → NodeTab remounts the editor (key=selected.id).
    selection.nodes = [{ id: 'node_2', selected: true, data: draft.node_2 }];
    rerender(<NodeTab wfId="wf_1" />);
    expect(screen.queryByText(/already used/i)).toBeNull();
    expect(screen.getByTestId('field-name-z')).toBeInTheDocument();
  });
});

describe('NodeTab — non-mirroring node has editable outputs', () => {
  beforeEach(() => {
    useWorkflowEditStore.setState({ draft: null, baseline: 'null', undoStack: [], redoStack: [] });
  });

  it('shows an Add field for outputs on a CodeNode', () => {
    seed(
      {
        node_2: {
          node_id: 'node_2',
          node_type: 'CodeNode',
          input_fields: {},
          output_fields: { out: { type: 'string', description: '' } },
        },
      },
      'node_2',
    );
    render(<NodeTab wfId="wf_1" />);
    expect(screen.getByTestId('add-field-output')).toBeInTheDocument();
    expect(screen.queryByTestId('outputs-mirror-caption')).toBeNull();
  });

  it('renders clearly bounded, independently collapsible inspector blocks', async () => {
    seed(
      {
        node_2: {
          node_id: 'node_2',
          node_type: 'CodeNode',
          node_name: 'transform',
          input_fields: { source: { type: 'string', value: '', reference: '' } },
          output_fields: { result: { type: 'string', description: '' } },
          node_config: { programming_language: 'python', process_fn: '' },
        },
      },
      'node_2',
    );
    render(<NodeTab wfId="wf_1" />);

    expect(screen.getByTestId('inspector-section-details')).toBeInTheDocument();
    expect(screen.getByTestId('fields-editor-input')).toBeInTheDocument();
    expect(screen.getByTestId('fields-editor-output')).toBeInTheDocument();

    const toggle = screen.getByRole('button', { name: 'Collapse Input fields' });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await userEvent.click(toggle);
    expect(screen.getByRole('button', { name: 'Expand Input fields' })).toHaveAttribute(
      'aria-expanded',
      'false',
    );
    expect(screen.getByTestId('field-card-source').closest('[hidden]')).not.toBeNull();
  });
});

// Reserved-name guard (naming-guidance patch). Kept in THIS file rather than a
// sibling so the shared `@xyflow/react` + `NodeConfigEditor` mocks aren't
// clobbered across files under isolate=false ([[feedback_vitest_isolate_false]]).
describe('NodeTab — reserved name field (Start/End read-only)', () => {
  beforeEach(() => {
    useWorkflowEditStore.setState({ draft: null, baseline: 'null', undoStack: [], redoStack: [] });
  });

  it('renders the StartNode name read-only and suppresses the boilerplate description + caption + config (Change 1 + 3)', () => {
    seed(
      {
        node_1: {
          node_id: 'node_1',
          node_type: 'StartNode',
          node_name: '__start__',
          input_fields: {},
          output_fields: {},
        },
      },
      'node_1',
    );
    render(<NodeTab wfId="wf_1" />);
    expect(screen.getByLabelText('Name')).toBeDisabled();
    // No reserved-name caption sub-text + no Description field for Start/End.
    expect(screen.queryByTestId('node-name-caption')).toBeNull();
    expect(screen.queryByLabelText('Description')).toBeNull();
    // No Config section for a config-less type.
    expect(screen.queryByText('Config')).toBeNull();
  });

  it('renders the EndNode name field read-only with no description/config', () => {
    seed(
      {
        node_9: {
          node_id: 'node_9',
          node_type: 'EndNode',
          node_name: '__end__',
          input_fields: {},
          output_fields: {},
        },
      },
      'node_9',
    );
    render(<NodeTab wfId="wf_1" />);
    expect(screen.getByLabelText('Name')).toBeDisabled();
    expect(screen.queryByLabelText('Description')).toBeNull();
  });

  it('keeps a CodeNode name + description editable and shows its config', () => {
    seed(
      {
        node_2: {
          node_id: 'node_2',
          node_type: 'CodeNode',
          node_name: 'transform',
          input_fields: {},
          output_fields: {},
        },
      },
      'node_2',
    );
    render(<NodeTab wfId="wf_1" />);
    expect(screen.getByLabelText('Name')).not.toBeDisabled();
    expect(screen.queryByTestId('node-name-caption')).toBeNull();
    // CodeNode keeps its Description field (non-reserved type).
    expect(screen.getByLabelText('Description')).toBeInTheDocument();
  });
});

// Loop control-flow nodes (UX-7): LoopBegin hides INPUT but shows OUTPUT
// read-only (engine-fixed { i, loop_output }); LoopEnd mirrors Parallel —
// neither fields nor config. NodeConfigEditor is stubbed to null at the top of
// this file, so the Config section's presence is detected via its "Config"
// header label, exactly as the reserved-name suite above does.
describe('NodeTab — Loop nodes fields/config gating', () => {
  beforeEach(() => {
    useWorkflowEditStore.setState({ draft: null, baseline: 'null', undoStack: [], redoStack: [] });
  });

  it('LoopBegin hides the input block and shows outputs read-only', () => {
    seed(
      {
        node_3: {
          node_id: 'node_3',
          node_type: 'LoopBeginNode',
          node_name: 'loop_start',
          input_fields: {},
          output_fields: {
            i: { type: 'integer' },
            loop_output: { type: 'array' },
          },
        },
      },
      'node_3',
    );
    render(<NodeTab wfId="wf_1" />);
    // Input block hidden, output block shown.
    expect(screen.queryByTestId('fields-editor-input')).toBeNull();
    expect(screen.getByTestId('fields-editor-output')).toBeInTheDocument();
    // Outputs are read-only: the Add control is disabled; the per-field NAME
    // inputs use readOnly (not disabled) so a fixed field name stays
    // selectable + keyboard-copyable.
    expect(screen.getByTestId('add-field-output')).toBeDisabled();
    expect(screen.getByTestId('field-name-i')).toHaveAttribute('readonly');
    expect(screen.getByTestId('field-name-loop_output')).toHaveAttribute('readonly');
  });

  it('LoopBegin PRESETS i + loop_output read-only even when the stored node lacks them, and backfills the draft', () => {
    // Stored node has NO output_fields — the inspector must still preset the
    // engine-fixed { loop_output, i } and write them into the draft so the
    // route Check passes.
    seed(
      {
        node_3: {
          node_id: 'node_3',
          node_type: 'LoopBeginNode',
          node_name: 'loop_start',
          input_fields: {},
          output_fields: {},
        },
      },
      'node_3',
    );
    render(<NodeTab wfId="wf_1" />);
    // Preset fields are shown, read-only (names readOnly so they're copyable).
    expect(screen.getByTestId('field-name-loop_output')).toHaveAttribute('readonly');
    expect(screen.getByTestId('field-name-i')).toHaveAttribute('readonly');
    expect(screen.getByTestId('add-field-output')).toBeDisabled();
    // Backfilled into the draft with the exact engine names + types.
    const draft = useWorkflowEditStore.getState().draft as Record<string, unknown>;
    const node = draft.node_3 as Record<string, unknown>;
    expect(node.output_fields).toEqual({
      loop_output: {
        type: 'array',
        description: 'Collected outputs from each iteration',
      },
      i: { type: 'integer', description: 'Current loop index' },
    });
  });

  it('LoopEnd hides both field blocks and renders no config section', () => {
    seed(
      {
        node_4: {
          node_id: 'node_4',
          node_type: 'LoopEndNode',
          node_name: 'loop_end',
          input_fields: {},
          output_fields: {},
          node_config: { loop_begin_node_id: 'node_3' },
        },
      },
      'node_4',
    );
    render(<NodeTab wfId="wf_1" />);
    expect(screen.queryByTestId('fields-editor-input')).toBeNull();
    expect(screen.queryByTestId('fields-editor-output')).toBeNull();
    // Config-less type → no Config section header.
    expect(screen.queryByText('Config')).toBeNull();
  });
});

// UX-14: fixed-output node types. The engine enforces an EXACT output schema
// (Node.check) for HTTPRequest/LoopBegin/etc.; the inspector PRESETS those
// fields + renders them read-only (greyed) and backfills the draft so the
// stored output_fields match the engine-enforced set.
describe('NodeTab — fixed output fields (HTTPRequestNode)', () => {
  beforeEach(() => {
    useWorkflowEditStore.setState({ draft: null, baseline: 'null', undoStack: [], redoStack: [] });
  });

  it('shows the 3 fixed output fields read-only (add + name + type disabled) and keeps inputs editable', () => {
    seed(
      {
        node_2: {
          node_id: 'node_2',
          node_type: 'HTTPRequestNode',
          node_name: 'fetch_user',
          input_fields: { user_id: { type: 'string', value: '', reference: '' } },
          output_fields: {},
        },
      },
      'node_2',
    );
    render(<NodeTab wfId="wf_1" />);
    // The 3 engine-fixed output fields are present.
    expect(screen.getByTestId('field-name-response_body')).toBeInTheDocument();
    expect(screen.getByTestId('field-name-status_code')).toBeInTheDocument();
    expect(screen.getByTestId('field-name-response_headers')).toBeInTheDocument();
    // Read-only: can't add or retype them; the NAME uses readOnly (not
    // disabled) so a fixed field name stays selectable + keyboard-copyable.
    expect(screen.getByTestId('add-field-output')).toBeDisabled();
    expect(screen.getByTestId('field-name-response_body')).toHaveAttribute('readonly');
    expect(screen.getByTestId('field-type-status_code')).toBeDisabled();
    // Inputs stay editable (HTTPRequest authors its own inputs).
    expect(screen.getByTestId('add-field-input')).not.toBeDisabled();
  });

  it('backfills the draft output_fields to the exact engine-fixed set', () => {
    seed(
      {
        node_2: {
          node_id: 'node_2',
          node_type: 'HTTPRequestNode',
          node_name: 'fetch_user',
          input_fields: {},
          // Intentionally wrong/stale stored outputs — must be overwritten.
          output_fields: { foo: { type: 'string', description: 'stale' } },
        },
      },
      'node_2',
    );
    render(<NodeTab wfId="wf_1" />);
    const draft = useWorkflowEditStore.getState().draft as Record<string, unknown>;
    const node = draft.node_2 as Record<string, unknown>;
    expect(node.output_fields).toEqual({
      response_body: { type: 'object', description: 'API response body' },
      status_code: { type: 'integer', description: 'HTTP status code' },
      response_headers: { type: 'object', description: 'Response headers' },
    });
  });

  it('does NOT backfill in read-only (pinned historical) mode', () => {
    seed(
      {
        node_2: {
          node_id: 'node_2',
          node_type: 'HTTPRequestNode',
          node_name: 'fetch_user',
          input_fields: {},
          output_fields: {},
        },
      },
      'node_2',
    );
    render(<NodeTab wfId="wf_1" readOnly />);
    const draft = useWorkflowEditStore.getState().draft as Record<string, unknown>;
    const node = draft.node_2 as Record<string, unknown>;
    // Draft untouched (no mutation of a viewed-only version).
    expect(node.output_fields).toEqual({});
    // But the preset is still DISPLAYED (read-only) so the user sees the format.
    expect(screen.getByTestId('field-name-response_body')).toBeInTheDocument();
  });
});
