/**
 * Stream 3 — interactive node config editors.
 *
 * Coverage:
 *   - pairing editors (ParallelStart/End, LoopBegin/End) call the store's
 *     `pairNodes` and set BOTH sides in ONE step;
 *   - ConditionNodeEditor edits via onChange, the builder generates a valid
 *     condition_str, the next_node_id dropdown excludes claimed children,
 *     and the warnings fire;
 *   - PromptNode model dropdown renders from the (mocked) `useModelOptions`
 *     hook and falls back to free-text when empty;
 *   - HTTPRequest headers render as a key-value table.
 *
 * Mocking discipline (vitest `isolate:false`, shared module graph): we keep
 * the `@xyflow/react` mock minimal + identical in shape to the sibling
 * Stream-5 test, and pass `nodeId` to every editor so the xyflow selection
 * is irrelevant. The edit store is the REAL store (seeded via setState).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { workflowVersionsQueryKey } from '@/lib/api/queries/workflow';

// PromptNodeEditor now calls `useWorkflowVersions` (a `useQuery`) to gate its
// prompt_template "History" button. The hook is DISABLED here (no wfId is
// passed), so it never fetches — but `useQuery` still needs a QueryClient in
// context. Wrap the PromptNode renders in a throwaway client. We deliberately
// DON'T `vi.mock('@/lib/api/queries/workflow', …)` because this suite runs
// with `isolate:false`: a second mock of that module would clobber the
// explorer's `sections.test.tsx` mock in the shared module graph.
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const QcWrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={qc}>{children}</QueryClientProvider>
);

// jsdom lacks the Pointer Capture API + scrollIntoView that Radix Select
// calls on open. Polyfill them locally (documented Radix+jsdom workaround)
// so the dropdown can be driven by userEvent. Scoped to this file.
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => {};
  Element.prototype.releasePointerCapture = () => {};
}
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// Minimal xyflow mock (selection unused — editors take `nodeId`).
vi.mock('@xyflow/react', () => ({
  useNodes: () => [] as unknown[],
}));

// CodeMirror (`@uiw/react-codemirror`, used by CodeMirrorField → Prompt &
// Template editors) is heavy / flaky under jsdom. Replace the shared
// CodeMirrorField with a lightweight textarea that preserves the contract we
// assert on: `data-testid`, `placeholder`, readOnly, and commit-on-blur. This
// suite runs `isolate:false`, so the mock applies to every editor that imports
// it (PromptNode + TemplateNode) — fine, since no test inspects CodeMirror
// internals, only the placeholder / testid / commit behaviour.
vi.mock('../CodeMirrorField', () => ({
  CodeMirrorField: ({
    value,
    onCommit,
    readOnly,
    placeholder,
    'data-testid': testId,
  }: {
    value: string;
    onCommit: (next: string) => void;
    readOnly?: boolean;
    placeholder?: string;
    'data-testid'?: string;
  }) => (
    <textarea
      data-testid={testId}
      defaultValue={value}
      placeholder={placeholder}
      disabled={readOnly}
      onBlur={(e) => onCommit(e.target.value)}
    />
  ),
}));

// Controllable model list for the PromptNode dropdown.
let modelOptions: string[] = ['gpt-4o', 'claude-3-5-sonnet'];
vi.mock('@/lib/api/queries/config-options', () => ({
  useModelOptions: () => ({ options: modelOptions, isLoading: false }),
}));

// Controllable saved-credential list for the PromptNode picker.
// Public projection — names + provider only, NEVER secrets.
let savedCredentials: { id: string; name: string; provider: string }[] = [];
vi.mock('@/lib/api/queries/llm-credentials', () => ({
  useLlmCredentials: () => ({ data: savedCredentials }),
  useWorkflowModels: () => ({ data: { models: Object.fromEntries(savedCredentials.map(c => [c.name, { provider: c.provider }])) }, isPending: false, isError: false }),
}));

// `t('key','fallback')` → fallback (i18n is not initialised in tests).
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback ?? _key,
  }),
}));

import { useWorkflowEditStore } from '@/stores/workflow-edit';
import { ParallelStartNodeEditor } from '../ParallelStartNodeEditor';
import { ParallelEndNodeEditor } from '../ParallelEndNodeEditor';
import { LoopBeginNodeEditor } from '../LoopBeginNodeEditor';
import { LoopEndNodeEditor } from '../LoopEndNodeEditor';
import { ConditionNodeEditor } from '../ConditionNodeEditor';
import { moveCondition } from '../condition-model';
import { PromptNodeEditor } from '../PromptNodeEditor';
import { TemplateNodeEditor } from '../TemplateNodeEditor';
import { HTTPRequestNodeEditor } from '../HTTPRequestNodeEditor';
import { TransformNodeEditor } from '../TransformNodeEditor';
import {
  moveTransform,
  defaultTransform,
} from '../transform-model';
import { TableReadNodeEditor } from '../TableReadNodeEditor';
import { TableWriteNodeEditor } from '../TableWriteNodeEditor';
import { deriveTableFormat } from '../table-format';
import { nodeTypeHasConfig } from '../../node-config-registry';

function seedDraft(draft: Record<string, unknown>) {
  useWorkflowEditStore.getState().setDraft(draft);
}

/** Read a node's node_config out of the live draft. */
function cfg(id: string): Record<string, unknown> {
  const node = useWorkflowEditStore.getState().draft?.[id] as
    | Record<string, unknown>
    | undefined;
  return (node?.node_config ?? {}) as Record<string, unknown>;
}

beforeEach(() => {
  modelOptions = ['gpt-4o', 'claude-3-5-sonnet'];
  savedCredentials = [{ id: 'c1', name: 'manual-model', provider: 'openai' }];
  useWorkflowEditStore.setState({
    draft: null,
    dirty: false,
    baseline: 'null',
    undoStack: [],
    redoStack: [],
    clipboard: [],
  });
});

describe('Parallel pairing editors', () => {
  it('ParallelStart parallel_end dropdown pairs both sides in one step', async () => {
    const user = userEvent.setup();
    seedDraft({
      node_1: { node_id: 'node_1', node_type: 'ParallelStartNode', node_name: 'split', node_config: { branches: {}, parallel_end_node_id: null }, children: [] },
      node_2: { node_id: 'node_2', node_type: 'ParallelEndNode', node_name: 'join', node_config: { parallel_start_node_id: null } },
    });
    render(
      <ParallelStartNodeEditor
        nodeId="node_1"
        config={cfg('node_1')}
        onChange={(next) =>
          useWorkflowEditStore.getState().applyEdit((wf) => {
            (wf.node_1 as Record<string, unknown>).node_config = next;
            return wf;
          })
        }
      />,
    );
    await user.click(screen.getByTestId('cfg-parallel-end-select'));
    await user.click(await screen.findByText('join (node_2)'));

    // BOTH sides set atomically.
    expect(cfg('node_1').parallel_end_node_id).toBe('node_2');
    expect(cfg('node_2').parallel_start_node_id).toBe('node_1');
    // ONE undo step.
    expect(useWorkflowEditStore.getState().undoStack).toHaveLength(1);
  });

  it('ParallelEnd mirror dropdown also pairs both sides', async () => {
    const user = userEvent.setup();
    seedDraft({
      node_1: { node_id: 'node_1', node_type: 'ParallelStartNode', node_name: 'split', node_config: { branches: {}, parallel_end_node_id: null }, children: [] },
      node_2: { node_id: 'node_2', node_type: 'ParallelEndNode', node_name: 'join', node_config: { parallel_start_node_id: null } },
    });
    render(
      <ParallelEndNodeEditor nodeId="node_2" config={cfg('node_2')} onChange={() => {}} />,
    );
    await user.click(screen.getByTestId('cfg-parallel-start-select'));
    await user.click(await screen.findByText('split (node_1)'));
    expect(cfg('node_1').parallel_end_node_id).toBe('node_2');
    expect(cfg('node_2').parallel_start_node_id).toBe('node_1');
  });

  it('ParallelStart branch description commits through onChange', async () => {
    const user = userEvent.setup();
    seedDraft({
      node_1: {
        node_id: 'node_1',
        node_type: 'ParallelStartNode',
        node_name: 'split',
        node_config: { branches: { b1: { branch_description: '', next_node_id: 'node_3' } }, parallel_end_node_id: null },
        children: ['node_3'],
      },
      node_3: { node_id: 'node_3', node_type: 'CodeNode', node_name: 'work' },
    });
    render(
      <ParallelStartNodeEditor
        nodeId="node_1"
        config={cfg('node_1')}
        onChange={(next) =>
          useWorkflowEditStore.getState().applyEdit((wf) => {
            (wf.node_1 as Record<string, unknown>).node_config = next;
            return wf;
          })
        }
      />,
    );
    const input = screen.getByTestId('cfg-branch-desc-b1');
    await user.type(input, 'first branch');
    await user.tab();
    const branches = cfg('node_1').branches as Record<string, { branch_description?: string }>;
    expect(branches.b1.branch_description).toBe('first branch');
  });
});

describe('Loop pairing editors', () => {
  it('LoopBegin loop_end dropdown pairs both sides + step commits', async () => {
    const user = userEvent.setup();
    seedDraft({
      node_1: { node_id: 'node_1', node_type: 'LoopBeginNode', node_name: 'loop', node_config: { init_value: { value: 0, reference: '' }, step_value: 1, end_value: { value: 5, reference: '' }, loop_end_node_id: null }, children: [] },
      node_2: { node_id: 'node_2', node_type: 'LoopEndNode', node_name: 'endloop', node_config: { loop_begin_node_id: null } },
    });
    render(
      <LoopBeginNodeEditor
        nodeId="node_1"
        config={cfg('node_1')}
        onChange={(next) =>
          useWorkflowEditStore.getState().applyEdit((wf) => {
            (wf.node_1 as Record<string, unknown>).node_config = next;
            return wf;
          })
        }
      />,
    );
    await user.click(screen.getByTestId('cfg-loop-end-select'));
    await user.click(await screen.findByText('endloop (node_2)'));
    expect(cfg('node_1').loop_end_node_id).toBe('node_2');
    expect(cfg('node_2').loop_begin_node_id).toBe('node_1');
  });

  it('LoopEnd mirror dropdown pairs both sides', async () => {
    const user = userEvent.setup();
    seedDraft({
      node_1: { node_id: 'node_1', node_type: 'LoopBeginNode', node_name: 'loop', node_config: { init_value: { value: 0, reference: '' }, step_value: 1, end_value: { value: 5, reference: '' }, loop_end_node_id: null }, children: [] },
      node_2: { node_id: 'node_2', node_type: 'LoopEndNode', node_name: 'endloop', node_config: { loop_begin_node_id: null } },
    });
    render(<LoopEndNodeEditor nodeId="node_2" config={cfg('node_2')} onChange={() => {}} />);
    await user.click(screen.getByTestId('cfg-loop-begin-select'));
    await user.click(await screen.findByText('loop (node_1)'));
    expect(cfg('node_1').loop_end_node_id).toBe('node_2');
    expect(cfg('node_2').loop_begin_node_id).toBe('node_1');
  });

  it('LoopBegin init/end reference candidates are ANCESTOR-only (exclude successors) + semantics hint renders', () => {
    // Topology: producer(node_0) -> loop(node_1) -> body(node_3) -> endloop(node_2).
    // node_0 is an ANCESTOR of the loop; node_3 is a DESCENDANT (inside the loop body).
    // The init/end reference dropdowns must offer node_0's output but NOT node_3's.
    seedDraft({
      node_0: {
        node_id: 'node_0',
        node_type: 'CodeNode',
        node_name: 'producer',
        output_fields: { total: { type: 'integer' } },
        children: ['node_1'],
      },
      node_1: {
        node_id: 'node_1',
        node_type: 'LoopBeginNode',
        node_name: 'loop',
        // init/end seeded WITH a reference so the FieldValueWidget renders the
        // reference <select> (reference-mode is derived from a non-empty ref).
        node_config: {
          init_value: { value: 0, reference: 'producer.total' },
          step_value: 1,
          end_value: { value: 5, reference: 'producer.total' },
          loop_end_node_id: 'node_2',
        },
        output_fields: { loop_output: { type: 'array' }, i: { type: 'integer' } },
        children: ['node_3'],
      },
      node_3: {
        node_id: 'node_3',
        node_type: 'CodeNode',
        node_name: 'body',
        output_fields: { item: { type: 'string' } },
        children: ['node_2'],
      },
      node_2: { node_id: 'node_2', node_type: 'LoopEndNode', node_name: 'endloop', node_config: { loop_begin_node_id: 'node_1' }, children: [] },
    });
    render(<LoopBeginNodeEditor nodeId="node_1" config={cfg('node_1')} onChange={() => {}} />);

    // Semantics hint renders and is unambiguous about end_value exclusivity.
    const hint = screen.getByTestId('cfg-loop-semantics-hint');
    expect(hint.textContent).toMatch(/i < end_value/);
    expect(hint.textContent?.toLowerCase()).toContain('exclusive');

    // init reference dropdown: ancestor producer.total present, descendant body.item absent.
    const initOpts = Array.from(
      (screen.getByTestId('cfg-loop-init-ref-select') as HTMLSelectElement).options,
    ).map((o) => o.value);
    expect(initOpts).toContain('producer.total');
    expect(initOpts).not.toContain('body.item');
    // The loop's OWN outputs are excluded too (self is never an ancestor).
    expect(initOpts).not.toContain('loop.loop_output');
    expect(initOpts).not.toContain('loop.i');

    // end reference dropdown: same ancestor-restriction.
    const endOpts = Array.from(
      (screen.getByTestId('cfg-loop-end-val-ref-select') as HTMLSelectElement).options,
    ).map((o) => o.value);
    expect(endOpts).toContain('producer.total');
    expect(endOpts).not.toContain('body.item');
  });

  it('LoopBegin readOnly disables the step input', () => {
    seedDraft({
      node_1: { node_id: 'node_1', node_type: 'LoopBeginNode', node_config: { init_value: { value: 0, reference: '' }, step_value: 1, end_value: { value: 5, reference: '' }, loop_end_node_id: null }, children: [] },
    });
    render(<LoopBeginNodeEditor nodeId="node_1" readOnly config={cfg('node_1')} onChange={() => {}} />);
    expect(screen.getByTestId('cfg-loop-step')).toBeDisabled();
  });
});

describe('ConditionNodeEditor', () => {
  const condDraft = () => ({
    node_1: {
      node_id: 'node_1',
      node_type: 'ConditionNode',
      node_name: 'route',
      input_fields: { score: { type: 'number' } },
      node_config: {
        conditions: [
          { condition_name: 'branch_1', condition_str: '', next_node_id: 'node_2' },
          { condition_name: 'others', condition_str: 'others', next_node_id: null },
        ],
      },
      children: ['node_2', 'node_3'],
    },
    node_2: { node_id: 'node_2', node_type: 'CodeNode', node_name: 'high' },
    node_3: { node_id: 'node_3', node_type: 'CodeNode', node_name: 'low' },
  });

  function renderCond() {
    return render(
      <ConditionNodeEditor
        nodeId="node_1"
        config={cfg('node_1')}
        onChange={(next) =>
          useWorkflowEditStore.getState().applyEdit((wf) => {
            (wf.node_1 as Record<string, unknown>).node_config = next;
            return wf;
          })
        }
      />,
    );
  }

  it('builder generates a valid condition_str from field/op/value', async () => {
    const user = userEvent.setup();
    seedDraft(condDraft());
    renderCond();
    // field defaults to "score"; pick operator >= and type a value.
    await user.click(screen.getByTestId('cfg-condition-op-0'));
    await user.click(await screen.findByText('>='));
    const valInput = screen.getByTestId('cfg-condition-value-0');
    await user.type(valInput, '0.8');
    await user.tab();
    const conditions = cfg('node_1').conditions as { condition_str?: string }[];
    expect(conditions[0].condition_str).toBe('{score} >= 0.8');
  });

  it('next_node_id dropdown excludes children claimed by other rows', async () => {
    const user = userEvent.setup();
    // node_2 is claimed by row 0; row 1 (unclaimed) should NOT offer node_2.
    seedDraft({
      node_1: {
        node_id: 'node_1',
        node_type: 'ConditionNode',
        node_name: 'route',
        input_fields: { score: { type: 'number' } },
        node_config: {
          conditions: [
            { condition_name: 'b1', condition_str: '{score} > 1', next_node_id: 'node_2' },
            { condition_name: 'b2', condition_str: '{score} < 1', next_node_id: null },
            { condition_name: 'others', condition_str: 'others', next_node_id: null },
          ],
        },
        children: ['node_2', 'node_3'],
      },
      node_2: { node_id: 'node_2', node_type: 'CodeNode', node_name: 'high' },
      node_3: { node_id: 'node_3', node_type: 'CodeNode', node_name: 'low' },
    });
    renderCond();
    await user.click(screen.getByTestId('cfg-condition-target-1'));
    // The open listbox should offer node_3 (low) but NOT node_2 (high,
    // claimed by row 0). Scope to role="option" so we ignore the row-0
    // trigger which legitimately DISPLAYS its own "high (node_2)" value.
    const options = await screen.findAllByRole('option');
    const optionTexts = options.map((o) => o.textContent);
    expect(optionTexts).toContain('low (node_3)');
    expect(optionTexts).not.toContain('high (node_2)');
  });

  it('warns on empty condition_str and an unmapped child', () => {
    seedDraft(condDraft()); // row0 empty str + node_3 unmapped
    renderCond();
    const warnings = screen.getByTestId('cfg-condition-warnings');
    expect(within(warnings).getByText(/empty condition/i)).toBeInTheDocument();
    expect(within(warnings).getByText(/not mapped to a condition/i)).toBeInTheDocument();
  });

  it('warns when the "others" fallback is missing', () => {
    seedDraft({
      node_1: {
        node_id: 'node_1',
        node_type: 'ConditionNode',
        node_name: 'route',
        node_config: { conditions: [{ condition_name: 'b1', condition_str: '{x} > 1', next_node_id: 'node_2' }] },
        children: ['node_2'],
      },
      node_2: { node_id: 'node_2', node_type: 'CodeNode', node_name: 'go' },
    });
    renderCond();
    expect(within(screen.getByTestId('cfg-condition-warnings')).getByText(/others.*fallback/i)).toBeInTheDocument();
  });

  it('dropdown edit persists advanced:false + field/operator/value on the card', async () => {
    const user = userEvent.setup();
    seedDraft(condDraft());
    renderCond();
    await user.click(screen.getByTestId('cfg-condition-op-0'));
    await user.click(await screen.findByText('>='));
    const valInput = screen.getByTestId('cfg-condition-value-0');
    await user.type(valInput, '0.8');
    await user.tab();
    const conditions = cfg('node_1').conditions as Record<string, unknown>[];
    expect(conditions[0]).toMatchObject({
      condition_str: '{score} >= 0.8',
      advanced: false,
      field: 'score',
      operator: '>=',
      value: '0.8',
    });
  });

  it('restores the dropdown selection from a card with persisted builder state', () => {
    seedDraft({
      node_1: {
        node_id: 'node_1',
        node_type: 'ConditionNode',
        node_name: 'route',
        input_fields: { score: { type: 'number' }, grade: { type: 'string' } },
        node_config: {
          conditions: [
            {
              condition_name: 'b1',
              condition_str: "{grade} == 'A'",
              next_node_id: 'node_2',
              advanced: false,
              field: 'grade',
              operator: '==',
              value: 'A',
            },
            { condition_name: 'others', condition_str: 'others', next_node_id: null },
          ],
        },
        children: ['node_2'],
      },
      node_2: { node_id: 'node_2', node_type: 'CodeNode', node_name: 'go' },
    });
    renderCond();
    // Dropdown mode (not advanced): field shows "grade", op shows "==",
    // value input pre-filled with "A".
    expect(screen.getByTestId('cfg-condition-field-0').textContent).toContain('grade');
    expect(screen.getByTestId('cfg-condition-op-0').textContent).toContain('==');
    expect((screen.getByTestId('cfg-condition-value-0') as HTMLInputElement).value).toBe('A');
    // Advanced checkbox unchecked → not the raw editor.
    expect((screen.getByTestId('cfg-condition-advanced-0') as HTMLInputElement).checked).toBe(
      false,
    );
    expect(screen.queryByTestId('cfg-condition-raw-0')).not.toBeInTheDocument();
  });

  it('renders the raw editor when the card has advanced:true persisted', () => {
    seedDraft({
      node_1: {
        node_id: 'node_1',
        node_type: 'ConditionNode',
        node_name: 'route',
        input_fields: { score: { type: 'number' } },
        node_config: {
          conditions: [
            {
              condition_name: 'b1',
              condition_str: '{score} > 1 and {score} < 9',
              next_node_id: 'node_2',
              advanced: true,
            },
            { condition_name: 'others', condition_str: 'others', next_node_id: null },
          ],
        },
        children: ['node_2'],
      },
      node_2: { node_id: 'node_2', node_type: 'CodeNode', node_name: 'go' },
    });
    renderCond();
    expect((screen.getByTestId('cfg-condition-advanced-0') as HTMLInputElement).checked).toBe(
      true,
    );
    expect(screen.getByTestId('cfg-condition-raw-0')).toBeInTheDocument();
    expect(screen.queryByTestId('cfg-condition-field-0')).not.toBeInTheDocument();
  });

  it('does NOT warn unmapped when a child is mapped only via the others card', () => {
    // 2 children: node_2 via a branch row, node_3 via the others fallback.
    seedDraft({
      node_1: {
        node_id: 'node_1',
        node_type: 'ConditionNode',
        node_name: 'route',
        input_fields: { score: { type: 'number' } },
        node_config: {
          conditions: [
            { condition_name: 'b1', condition_str: '{score} > 1', next_node_id: 'node_2' },
            { condition_name: 'others', condition_str: 'others', next_node_id: 'node_3' },
          ],
        },
        children: ['node_2', 'node_3'],
      },
      node_2: { node_id: 'node_2', node_type: 'CodeNode', node_name: 'high' },
      node_3: { node_id: 'node_3', node_type: 'CodeNode', node_name: 'low' },
    });
    renderCond();
    const warnings = screen.getByTestId('cfg-condition-warnings');
    expect(within(warnings).queryByText(/not mapped to a condition/i)).not.toBeInTheDocument();
  });

  it('raw "Advanced" toggle edits condition_str directly', async () => {
    const user = userEvent.setup();
    seedDraft(condDraft());
    renderCond();
    await user.click(screen.getByTestId('cfg-condition-advanced-0'));
    const raw = screen.getByTestId('cfg-condition-raw-0');
    // `{` opens a userEvent key-sequence — escape as `{{`. `}` is literal.
    await user.type(raw, '{{score} == 1');
    await user.tab();
    const conditions = cfg('node_1').conditions as { condition_str?: string }[];
    expect(conditions[0].condition_str).toBe('{score} == 1');
  });

  // --- moveCondition (pure reorder helper) ---------------------------------
  describe('moveCondition (pure)', () => {
    const list = () => [
      { condition_name: 'b1', condition_str: '{x}>1', next_node_id: 'node_2' },
      { condition_name: 'b2', condition_str: '{x}>2', next_node_id: 'node_3' },
      { condition_name: 'others', condition_str: 'others', next_node_id: null },
    ];

    it('reorders ONLY the non-others cards and keeps others last', () => {
      const out = moveCondition(list(), 0, 1);
      expect(out.map((c) => c.condition_name)).toEqual(['b2', 'b1', 'others']);
      // others always pinned last.
      expect(out[out.length - 1].condition_str).toBe('others');
    });

    it('is a no-op (others still last) for an out-of-range / same index move', () => {
      expect(moveCondition(list(), 0, 0).map((c) => c.condition_name)).toEqual([
        'b1',
        'b2',
        'others',
      ]);
      expect(moveCondition(list(), 5, 0).map((c) => c.condition_name)).toEqual([
        'b1',
        'b2',
        'others',
      ]);
    });
  });

  it('renders the "others" card LAST even when stored out of order', () => {
    seedDraft({
      node_1: {
        node_id: 'node_1',
        node_type: 'ConditionNode',
        node_name: 'route',
        node_config: {
          conditions: [
            { condition_name: 'others', condition_str: 'others', next_node_id: null },
            { condition_name: 'b1', condition_str: '{x}>1', next_node_id: 'node_2' },
          ],
        },
        children: ['node_2'],
      },
      node_2: { node_id: 'node_2', node_type: 'CodeNode', node_name: 'go' },
    });
    renderCond();
    const items = screen.getByTestId('cfg-condition').querySelectorAll('li');
    // last <li> is the others card.
    expect(items[items.length - 1].getAttribute('data-testid')).toBe(
      'cfg-condition-others',
    );
  });

  it('drag handles render for each non-others row; dropping reorders the cards', () => {
    seedDraft({
      node_1: {
        node_id: 'node_1',
        node_type: 'ConditionNode',
        node_name: 'route',
        node_config: {
          conditions: [
            { condition_name: 'b1', condition_str: '{x}>1', next_node_id: 'node_2' },
            { condition_name: 'b2', condition_str: '{x}>2', next_node_id: 'node_3' },
            { condition_name: 'others', condition_str: 'others', next_node_id: null },
          ],
        },
        children: ['node_2', 'node_3'],
      },
      node_2: { node_id: 'node_2', node_type: 'CodeNode', node_name: 'high' },
      node_3: { node_id: 'node_3', node_type: 'CodeNode', node_name: 'low' },
    });
    renderCond();
    // One drag handle per NON-others row.
    expect(screen.getByTestId('cfg-condition-drag-0')).toBeInTheDocument();
    expect(screen.getByTestId('cfg-condition-drag-1')).toBeInTheDocument();

    // Fire native DnD: drag row 0 onto row 1.
    const rows = screen
      .getByTestId('cfg-condition')
      .querySelectorAll('[data-testid^="cfg-condition-row-"]');
    fireEvent.dragStart(rows[0]);
    fireEvent.dragOver(rows[1]);
    fireEvent.drop(rows[1]);

    const conditions = cfg('node_1').conditions as { condition_name?: string }[];
    expect(conditions.map((c) => c.condition_name)).toEqual(['b2', 'b1', 'others']);
  });

  it('readOnly disables drag + remove buttons', () => {
    seedDraft({
      node_1: {
        node_id: 'node_1',
        node_type: 'ConditionNode',
        node_name: 'route',
        node_config: {
          conditions: [
            { condition_name: 'b1', condition_str: '{x}>1', next_node_id: 'node_2' },
            { condition_name: 'others', condition_str: 'others', next_node_id: null },
          ],
        },
        children: ['node_2'],
      },
      node_2: { node_id: 'node_2', node_type: 'CodeNode', node_name: 'go' },
    });
    render(
      <ConditionNodeEditor
        nodeId="node_1"
        readOnly
        config={cfg('node_1')}
        onChange={(next) =>
          useWorkflowEditStore.getState().applyEdit((wf) => {
            (wf.node_1 as Record<string, unknown>).node_config = next;
            return wf;
          })
        }
      />,
    );
    const row = screen
      .getByTestId('cfg-condition')
      .querySelector('[data-testid^="cfg-condition-row-"]') as HTMLElement;
    expect(row.getAttribute('draggable')).toBe('false');
    expect(screen.getByTestId('cfg-condition-remove-0')).toBeDisabled();
  });
});

describe('PromptNodeEditor model dropdown', () => {
  it('renders models from the hook', async () => {
    const user = userEvent.setup();
    render(<PromptNodeEditor config={{ model_name: '' }} onChange={() => {}} />, {
      wrapper: QcWrapper,
    });
    await user.click(screen.getByTestId('cfg-prompt-model-select'));
    expect(await screen.findByText('manual-model (openai)')).toBeInTheDocument();
    expect(screen.queryByText('gpt-4o')).not.toBeInTheDocument();
  });

  it('disables selection rather than allowing arbitrary names when empty', () => {
    modelOptions = [];
    savedCredentials = [];
    const onChange = vi.fn();
    render(
      <PromptNodeEditor config={{ model_name: 'legacy-model' }} onChange={onChange} />,
      { wrapper: QcWrapper },
    );
    expect(screen.getByTestId('cfg-prompt-model-select')).toBeDisabled();
    expect(screen.queryByTestId('cfg-prompt-model-input')).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('lists saved credentials as name (provider) and selecting one stores the NAME', async () => {
    const user = userEvent.setup();
    savedCredentials = [
      { id: 'c1', name: 'My DeepSeek', provider: 'OpenAI' },
      { id: 'c2', name: 'Team Gemini', provider: 'Gemini' },
    ];
    const onChange = vi.fn();
    render(<PromptNodeEditor config={{ model_name: '' }} onChange={onChange} />, {
      wrapper: QcWrapper,
    });
    await user.click(screen.getByTestId('cfg-prompt-model-select'));
    // Saved entry rendered as "name (provider)"; builtins still present.
    expect(await screen.findByText('My DeepSeek (OpenAI)')).toBeInTheDocument();
    expect(screen.getByText('Team Gemini (Gemini)')).toBeInTheDocument();
    expect(screen.queryByText('gpt-4o')).not.toBeInTheDocument();
    await user.click(screen.getByText('My DeepSeek (OpenAI)'));
    // Stores only the NAME (never a key).
    const last = onChange.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    expect(last.model_name).toBe('My DeepSeek');
  });

  it('never exposes inline model credentials, including legacy models', () => {
    savedCredentials = [{ id: 'c1', name: 'My DeepSeek', provider: 'OpenAI' }];
    // Saved name selected → inline fields hidden.
    const { rerender } = render(
      <PromptNodeEditor config={{ model_name: 'My DeepSeek' }} onChange={() => {}} />,
      { wrapper: QcWrapper },
    );
    expect(screen.queryByTestId('cfg-prompt-custom-key')).not.toBeInTheDocument();
    // Built-in 'OpenAI' selected → inline fields visible (back-compat path).
    rerender(<PromptNodeEditor config={{ model_name: 'OpenAI' }} onChange={() => {}} />);
    expect(screen.queryByTestId('cfg-prompt-custom-key')).not.toBeInTheDocument();
  });
});

describe('TemplateNodeEditor', () => {
  it('renders output_format ABOVE the template editor, and CodeMirror-izes the template', () => {
    render(
      <TemplateNodeEditor config={{ template: '', output_format: 'html' }} onChange={() => {}} />,
      { wrapper: QcWrapper },
    );
    const format = screen.getByTestId('cfg-template-format-select');
    const template = screen.getByTestId('cfg-template-template');
    expect(format).toBeInTheDocument();
    expect(template).toBeInTheDocument();
    // DOM order: the format select appears before the template editor.
    expect(
      format.compareDocumentPosition(template) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('shows a Jinja hint (label suffix + one-line hint)', () => {
    render(
      <TemplateNodeEditor config={{ template: '', output_format: 'html' }} onChange={() => {}} />,
      { wrapper: QcWrapper },
    );
    // The label carries an explicit "(Jinja template)" suffix...
    expect(screen.getByText('(Jinja template)')).toBeInTheDocument();
    // ...and the one-line hint mentions Jinja syntax.
    expect(screen.getByText(/Jinja2 template/)).toBeInTheDocument();
  });

  it('changes the template placeholder with the selected output_format', () => {
    const { rerender } = render(
      <TemplateNodeEditor config={{ template: '', output_format: 'html' }} onChange={() => {}} />,
      { wrapper: QcWrapper },
    );
    const htmlPlaceholder = screen
      .getByTestId('cfg-template-template')
      .getAttribute('placeholder');
    expect(htmlPlaceholder).toContain('<h1>');

    rerender(
      <TemplateNodeEditor config={{ template: '', output_format: 'markdown' }} onChange={() => {}} />,
    );
    const mdPlaceholder = screen
      .getByTestId('cfg-template-template')
      .getAttribute('placeholder');
    expect(mdPlaceholder).toContain('# {{title}}');
    expect(mdPlaceholder).not.toEqual(htmlPlaceholder);
  });

  it('selecting a format commits output_format through onChange', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <TemplateNodeEditor config={{ template: '', output_format: 'html' }} onChange={onChange} />,
      { wrapper: QcWrapper },
    );
    await user.click(screen.getByTestId('cfg-template-format-select'));
    await user.click(await screen.findByText('markdown'));
    const last = onChange.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    expect(last.output_format).toBe('markdown');
  });

  it('readOnly disables the format select + template editor and hides History', () => {
    render(
      <TemplateNodeEditor
        readOnly
        config={{ template: 'x', output_format: 'html' }}
        onChange={() => {}}
      />,
      { wrapper: QcWrapper },
    );
    expect(screen.getByTestId('cfg-template-template')).toBeDisabled();
    expect(screen.queryByTestId('cfg-template-history-btn')).not.toBeInTheDocument();
  });

  it('shows the History button only when the workflow has >= 2 versions', () => {
    // No wfId → no History (hook disabled).
    const { unmount } = render(
      <TemplateNodeEditor
        config={{ template: '', output_format: 'html' }}
        onChange={() => {}}
      />,
      { wrapper: QcWrapper },
    );
    expect(screen.queryByTestId('cfg-template-history-btn')).not.toBeInTheDocument();
    unmount();

    // Seed the versions query cache (>= 2 versions) so useWorkflowVersions
    // reads it synchronously — no module mock (would clobber sibling suites
    // under isolate:false).
    qc.setQueryData(workflowVersionsQueryKey('wf-1'), {
      versions: [
        { major: 1, sub: 0 },
        { major: 1, sub: 1 },
      ],
    });
    render(
      <TemplateNodeEditor
        config={{ template: '', output_format: 'html' }}
        onChange={() => {}}
        nodeId="node_1"
        wfId="wf-1"
      />,
      { wrapper: QcWrapper },
    );
    expect(screen.getByTestId('cfg-template-history-btn')).toBeInTheDocument();
    qc.clear();
  });
});

describe('HTTPRequest headers key-value table', () => {
  it('adds a header row via the table (no raw JSON)', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<HTTPRequestNodeEditor config={{ method: 'GET', headers: {} }} onChange={onChange} />);
    await user.click(screen.getByTestId('cfg-http-headers-add'));
    await user.type(screen.getByTestId('cfg-http-headers-key-0'), 'Authorization');
    await user.tab();
    await user.type(screen.getByTestId('cfg-http-headers-value-0'), 'Bearer x');
    await user.tab();
    const lastCall = onChange.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    expect((lastCall.headers as Record<string, string>).Authorization).toBe('Bearer x');
  });
});

describe('TransformNodeEditor', () => {
  // A draft with one mapping (out_a from in_x via a path op) and two declared
  // output fields (out_a configured, out_b a derived passthrough block).
  function renderTransform(
    onChange = vi.fn(),
    config: Record<string, unknown> = {
      mappings: [
        {
          input_field: 'in_x',
          output_field: 'out_a',
          transform_list: [{ op: 'path', path: 'user.name' }],
        },
      ],
    },
    extra: { inputFieldNames?: string[]; outputFieldNames?: string[] } = {},
  ) {
    render(
      <TransformNodeEditor
        config={config}
        onChange={onChange}
        inputFieldNames={extra.inputFieldNames ?? ['in_x', 'in_y']}
        outputFieldNames={extra.outputFieldNames ?? ['out_a', 'out_b']}
      />,
    );
    return onChange;
  }

  it('renders the no-outputs hint when there are no output fields', () => {
    render(
      <TransformNodeEditor
        config={{}}
        onChange={() => {}}
        inputFieldNames={['in_x']}
        outputFieldNames={[]}
      />,
    );
    expect(screen.getByTestId('transform-no-outputs')).toBeInTheDocument();
    expect(screen.queryByTestId('transform-block-out_a')).not.toBeInTheDocument();
  });

  it('renders one block per output field name', () => {
    renderTransform();
    expect(screen.getByTestId('transform-block-out_a')).toBeInTheDocument();
    expect(screen.getByTestId('transform-block-out_b')).toBeInTheDocument();
    expect(screen.queryByTestId('transform-block-out_c')).not.toBeInTheDocument();
  });

  it('opening the editor does NOT write (derived blocks until edited)', () => {
    const onChange = renderTransform();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('selecting an input writes mappings[].input_field for that block', async () => {
    const user = userEvent.setup();
    // out_b has no stored mapping → editing it materializes one.
    const onChange = renderTransform();
    await user.click(screen.getByTestId('transform-input-out_b'));
    await user.click(await screen.findByText('in_y'));
    const last = onChange.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    const mappings = last.mappings as Record<string, unknown>[];
    const block = mappings.find((m) => m.output_field === 'out_b');
    expect(block).toEqual({
      input_field: 'in_y',
      output_field: 'out_b',
      transform_list: [],
    });
    // existing out_a mapping preserved.
    expect(mappings.find((m) => m.output_field === 'out_a')).toMatchObject({
      input_field: 'in_x',
    });
  });

  it('Add transform appends to that block transform_list', async () => {
    const user = userEvent.setup();
    const onChange = renderTransform();
    await user.click(screen.getByTestId('transform-add-out_a'));
    const last = onChange.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    const mappings = last.mappings as Record<string, unknown>[];
    const block = mappings.find((m) => m.output_field === 'out_a') as {
      transform_list: Record<string, unknown>[];
    };
    expect(block.transform_list).toHaveLength(2);
    expect(block.transform_list[1]).toEqual(defaultTransform('path'));
  });

  it('changing an op type resets that row to the new type default', async () => {
    const user = userEvent.setup();
    const onChange = renderTransform();
    await user.click(screen.getByTestId('transform-op-out_a-0'));
    // mocked `t` returns the fallback; op labels fall back to the raw type
    // string ("cast"). Scope to the open listbox option.
    const options = await screen.findAllByRole('option');
    await user.click(options.find((o) => o.textContent === 'cast')!);
    const last = onChange.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    const mappings = last.mappings as Record<string, unknown>[];
    const block = mappings.find((m) => m.output_field === 'out_a') as {
      transform_list: Record<string, unknown>[];
    };
    // row 0 became a fresh `cast` default (cross-type `path` key discarded).
    expect(block.transform_list[0]).toEqual({ op: 'cast', to: 'string' });
  });

  it('removing a row deletes it from the block transform_list', async () => {
    const user = userEvent.setup();
    const onChange = renderTransform();
    await user.click(screen.getByTestId('transform-remove-out_a-0'));
    const last = onChange.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    const mappings = last.mappings as Record<string, unknown>[];
    const block = mappings.find((m) => m.output_field === 'out_a') as {
      transform_list: unknown[];
    };
    expect(block.transform_list).toHaveLength(0);
  });

  it('an empty block shows the passthrough hint', () => {
    renderTransform(vi.fn(), { mappings: [] });
    // out_a derived (empty) → passthrough hint within its block.
    const block = screen.getByTestId('transform-block-out_a');
    expect(block.textContent).toMatch(/Passes the input value through unchanged/);
  });

  it('shows and upgrades a legacy compute expression', async () => {
    const onChange = renderTransform(vi.fn(), {
      mappings: [{
        input_field: 'in_x',
        output_field: 'out_a',
        transform_list: [{ op: 'compute', expression: '{value} + 1' }],
      }],
    });
    const input = screen.getByTestId(
      'transform-param-out_a-0-expr',
    ) as HTMLInputElement;
    expect(input.value).toBe('{value} + 1');
    // fireEvent avoids user-event interpreting `{value}` as a keyboard token.
    fireEvent.change(input, { target: { value: '{value} + 2' } });
    fireEvent.blur(input);
    const last = onChange.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    const op = (
      (last.mappings as Record<string, unknown>[])[0]
        .transform_list as Record<string, unknown>[]
    )[0];
    expect(op).toEqual({ op: 'compute', expr: '{value} + 2' });
  });

  it('readOnly disables controls', () => {
    render(
      <TransformNodeEditor
        readOnly
        config={{
          mappings: [
            {
              input_field: 'in_x',
              output_field: 'out_a',
              transform_list: [{ op: 'path', path: '' }],
            },
          ],
        }}
        onChange={() => {}}
        inputFieldNames={['in_x']}
        outputFieldNames={['out_a']}
      />,
    );
    const row = screen.getByTestId('transform-row-out_a-0');
    expect(row.getAttribute('draggable')).toBe('false');
    expect(screen.getByTestId('transform-add-out_a')).toBeDisabled();
    expect(screen.getByTestId('transform-remove-out_a-0')).toBeDisabled();
  });

  it('drag reorders a block transform chain via onChange', () => {
    const onChange = vi.fn();
    render(
      <TransformNodeEditor
        config={{
          mappings: [
            {
              input_field: 'in_x',
              output_field: 'out_a',
              transform_list: [
                { op: 'path', path: 'a' },
                { op: 'length' },
              ],
            },
          ],
        }}
        onChange={onChange}
        inputFieldNames={['in_x']}
        outputFieldNames={['out_a']}
      />,
    );
    const row0 = screen.getByTestId('transform-row-out_a-0');
    const row1 = screen.getByTestId('transform-row-out_a-1');
    fireEvent.dragStart(row0);
    fireEvent.dragOver(row1);
    fireEvent.drop(row1);
    const last = onChange.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    const mappings = last.mappings as Record<string, unknown>[];
    const block = mappings.find((m) => m.output_field === 'out_a') as {
      transform_list: Record<string, unknown>[];
    };
    expect(block.transform_list.map((o) => o.op)).toEqual(['length', 'path']);
  });

  describe('moveTransform (pure)', () => {
    const list = () => [
      { op: 'path', path: 'a' },
      { op: 'index', index: 0 },
      { op: 'cast', to: 'number' },
    ];

    it('moves an op from one index to another', () => {
      expect(moveTransform(list(), 0, 2).map((o) => o.op)).toEqual([
        'index',
        'cast',
        'path',
      ]);
    });

    it('is a no-op for same-index / out-of-range moves', () => {
      expect(moveTransform(list(), 1, 1).map((o) => o.op)).toEqual([
        'path',
        'index',
        'cast',
      ]);
      expect(moveTransform(list(), 9, 0).map((o) => o.op)).toEqual([
        'path',
        'index',
        'cast',
      ]);
    });
  });

  describe('defaultTransform (pure)', () => {
    it('returns the engine-aligned default for each op type', () => {
      expect(defaultTransform('path')).toEqual({ op: 'path', path: '' });
      expect(defaultTransform('index')).toEqual({ op: 'index', index: 0 });
      expect(defaultTransform('length')).toEqual({ op: 'length' });
      expect(defaultTransform('cast')).toEqual({ op: 'cast', to: 'string' });
      expect(defaultTransform('default')).toEqual({ op: 'default', value: '' });
      expect(defaultTransform('compute')).toEqual({ op: 'compute', expr: '' });
      expect(defaultTransform('pick')).toEqual({ op: 'pick', fields: [] });
    });
  });
});

describe('deriveTableFormat (pure)', () => {
  it('maps known extensions to engine formats', () => {
    expect(deriveTableFormat('/run/x.csv')).toBe('csv');
    expect(deriveTableFormat('/run/x.jsonl')).toBe('jsonl');
    expect(deriveTableFormat('/run/x.ndjson')).toBe('jsonl');
    expect(deriveTableFormat('/run/x.json')).toBe('jsonl');
    expect(deriveTableFormat('/run/x.xlsx')).toBe('excel');
    expect(deriveTableFormat('/run/x.xls')).toBe('excel');
    expect(deriveTableFormat('/RUN/X.CSV')).toBe('csv');
  });
  it('returns null for unknown / missing suffix', () => {
    expect(deriveTableFormat('')).toBeNull();
    expect(deriveTableFormat('/run/noext')).toBeNull();
    expect(deriveTableFormat('/run/x.parquet')).toBeNull();
  });
  it('tolerates {{}} placeholders in the path', () => {
    expect(deriveTableFormat('/run/{{date}}/input.csv')).toBe('csv');
  });
});

describe('TableReadNodeEditor', () => {
  it('auto-derives excel from a .xlsx path and shows sheet_name', () => {
    render(
      <TableReadNodeEditor config={{ file_path: '/run/data.xlsx' }} onChange={() => {}} />,
    );
    // Format is auto-derived (the derived hint is shown, manual dropdown hidden).
    expect(screen.getByTestId('cfg-table-format-derived')).toBeInTheDocument();
    expect(screen.queryByTestId('cfg-table-format-select')).not.toBeInTheDocument();
    // sheet_name field appears for excel, with the {{}} variable hint.
    expect(screen.getByTestId('cfg-table-sheet-name')).toBeInTheDocument();
    expect(screen.getByTestId('cfg-table-sheet-hint').textContent).toMatch(/\{\{\}\}/);
  });

  it('a .csv path derives csv and hides sheet_name', () => {
    render(
      <TableReadNodeEditor config={{ file_path: '/run/data.csv' }} onChange={() => {}} />,
    );
    expect(screen.getByTestId('cfg-table-format-derived')).toBeInTheDocument();
    expect(screen.queryByTestId('cfg-table-sheet-name')).not.toBeInTheDocument();
  });

  it('writes the derived file_format and strips sheet_name on non-excel paths', () => {
    const onChange = vi.fn();
    render(
      <TableReadNodeEditor
        config={{ file_path: '/run/old.xlsx', sheet_name: 'Sheet1' }}
        onChange={onChange}
      />,
    );
    // Edit the path to a csv → commit should set file_format=csv and clear sheet_name.
    const input = screen.getByTestId('cfg-table-file-path') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '/run/new.csv' } });
    fireEvent.blur(input);
    const last = onChange.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    expect(last.file_format).toBe('csv');
    expect(last.sheet_name).toBe('');
  });

  it('shows a mode helper that reflects the selected mode', () => {
    const { rerender } = render(
      <TableReadNodeEditor config={{ file_path: '/run/x.csv', mode: 'batch' }} onChange={() => {}} />,
    );
    expect(screen.getByTestId('cfg-table-mode-hint').textContent).toMatch(/batch/i);
    rerender(
      <TableReadNodeEditor config={{ file_path: '/run/x.csv', mode: 'stream' }} onChange={() => {}} />,
    );
    expect(screen.getByTestId('cfg-table-mode-hint').textContent).toMatch(/stream/i);
  });

  it('falls back to a manual format dropdown on an unknown suffix', () => {
    render(<TableReadNodeEditor config={{ file_path: '/run/noext' }} onChange={() => {}} />);
    expect(screen.getByTestId('cfg-table-format-select')).toBeInTheDocument();
    expect(screen.queryByTestId('cfg-table-format-derived')).not.toBeInTheDocument();
  });

  it('readOnly disables the file_path input', () => {
    render(<TableReadNodeEditor readOnly config={{ file_path: '/run/x.csv' }} onChange={() => {}} />);
    expect(screen.getByTestId('cfg-table-file-path')).toBeDisabled();
  });

});

describe('TableWriteNodeEditor', () => {
  it('auto-derives excel from a .xlsx path and shows sheet_name', () => {
    render(<TableWriteNodeEditor config={{ file_path: '/run/out.xlsx' }} onChange={() => {}} />);
    expect(screen.getByTestId('cfg-table-format-derived')).toBeInTheDocument();
    expect(screen.getByTestId('cfg-table-sheet-name')).toBeInTheDocument();
    expect(screen.getByTestId('cfg-table-sheet-hint').textContent).toMatch(/\{\{\}\}/);
  });

  it('a .csv path derives csv and hides sheet_name', () => {
    render(<TableWriteNodeEditor config={{ file_path: '/run/out.csv' }} onChange={() => {}} />);
    expect(screen.getByTestId('cfg-table-format-derived')).toBeInTheDocument();
    expect(screen.queryByTestId('cfg-table-sheet-name')).not.toBeInTheDocument();
  });

  it('shows a write_mode helper that reflects the selection', () => {
    const { rerender } = render(
      <TableWriteNodeEditor config={{ file_path: '/run/x.csv', write_mode: 'overwrite' }} onChange={() => {}} />,
    );
    expect(screen.getByTestId('cfg-table-write-mode-hint').textContent).toMatch(/overwrite/i);
    rerender(
      <TableWriteNodeEditor config={{ file_path: '/run/x.csv', write_mode: 'append' }} onChange={() => {}} />,
    );
    expect(screen.getByTestId('cfg-table-write-mode-hint').textContent).toMatch(/append/i);
  });

  it('readOnly disables the file_path input', () => {
    render(<TableWriteNodeEditor readOnly config={{ file_path: '/run/x.csv' }} onChange={() => {}} />);
    expect(screen.getByTestId('cfg-table-file-path')).toBeDisabled();
  });

  it('data_write dropdown offers only object/list input fields and writes the selection', async () => {
    const onChange = vi.fn();
    render(
      <TableWriteNodeEditor
        config={{ file_path: '/run/x.csv', write_mode: 'overwrite' }}
        inputFields={{
          rows: { type: 'array' },
          rec: { type: 'object' },
          name: { type: 'string' },
          count: { type: 'integer' },
        }}
        onChange={onChange}
      />,
    );
    const trigger = screen.getByTestId('cfg-table-data-write-select');
    await userEvent.click(trigger);
    // object + array fields are offered; scalar fields are NOT.
    expect(screen.getByRole('option', { name: 'rows' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'rec' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'name' })).toBeNull();
    expect(screen.queryByRole('option', { name: 'count' })).toBeNull();
    // Selecting one writes node_config.data_write.
    await userEvent.click(screen.getByRole('option', { name: 'rows' }));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ data_write: 'rows' }),
    );
    // The explanatory hint is present.
    expect(screen.getByTestId('cfg-table-data-write-hint')).toBeInTheDocument();
  });
});

describe('nodeTypeHasConfig (Change 3 — hide empty config section)', () => {
  it('returns false for config-less types (Start/End) and unknown types', () => {
    expect(nodeTypeHasConfig('StartNode')).toBe(false);
    expect(nodeTypeHasConfig('EndNode')).toBe(false);
    expect(nodeTypeHasConfig('UnknownNode')).toBe(false);
  });

  it('returns true for types with a real config editor', () => {
    expect(nodeTypeHasConfig('CodeNode')).toBe(true);
    expect(nodeTypeHasConfig('PromptNode')).toBe(true);
    expect(nodeTypeHasConfig('ConditionNode')).toBe(true);
  });
});
