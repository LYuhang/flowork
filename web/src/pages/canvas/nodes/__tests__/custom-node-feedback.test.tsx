/**
 * Stream 8 — CustomNode feedback affordances:
 *   - a ⚠ warning badge in the header when `data.__warnings__` is non-empty
 *     (none when empty);
 *   - the source/target Handles carry the discoverability affordance class
 *     (enlarged hit-area + grow-on-hover + cursor-crosshair).
 *
 * The execution ring is folded into
 * this file deliberately: a per-node `EXEC_UPDATE{node_id,status}` frame in the
 * exec-stream store lights the matching node's ring + header indicator
 * (running → pulsing-blue + spinner; completed → green + check; error → red + a
 * ✕ that surfaces the node error). `reset()` clears the rings; the per-node
 * store selector means a sibling's transition does NOT re-render this memo'd
 * card. These tests SHARE this file's single `@xyflow/react` mock — under
 * vitest isolate:false a SECOND file mocking `@xyflow/react` collides in the
 * shared module graph (one file's hoisted factory loses, the real provider-less
 * Handle renders and throws), so these cases live here, not in a sibling file
 * (feedback_vitest_isolate_false).
 *
 * We stub the xyflow `Handle` so it renders its className into the DOM (the
 * real Handle needs a ReactFlow provider) — that lets us assert OUR styling
 * without standing up a flow runtime.
 */
import React, { Profiler } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import { act } from 'react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import i18n from 'i18next';
import en from '@/lib/i18n/locales/en.json';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router';
import { NODE_LABELS } from '../NODE_TYPES';

// NOTE: this @xyflow/react mock is byte-identical to the one in
// canvas-feedback.test.tsx. Under vitest `isolate:false` sibling files share
// the module graph, so a DIVERGENT mock would clobber the other file's
// expectations (see feedback_vitest_isolate_false). Keep the two in sync.
vi.mock('@xyflow/react', () => ({
  ReactFlow: ({ children }: { children?: React.ReactNode }) => (
    <div data-testid="rf">{children}</div>
  ),
  Background: () => null,
  Controls: () => null,
  MiniMap: () => null,
  Handle: ({
    type,
    className,
    id,
    isConnectable,
  }: {
    type: string;
    className?: string;
    id?: string;
    isConnectable?: boolean;
  }) => (
    <div
      data-handle={type}
      data-handle-id={id}
      data-connectable={isConnectable === false ? 'false' : undefined}
      className={className}
    />
  ),
  Position: { Left: 'left', Right: 'right', Bottom: 'bottom' },
  useNodesState: (init: unknown[]) => [init, vi.fn(), vi.fn()],
  useEdgesState: (init: unknown[]) => [init, vi.fn(), vi.fn()],
  useReactFlow: () => ({ screenToFlowPosition: (p: unknown) => p }),
}));

import {
  CustomNode,
  HANDLE_CLASS,
  nodeHoverSuppressed,
} from '@/pages/canvas/nodes/CustomNode';
import { useExecStreamStore } from '@/stores/exec-stream';

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

// CustomNode is `memo`-wrapped; render via the NodeProps shape it consumes.
// CustomNode is `memo(NodeProps => Element)`; xyflow's NodeProps is wider than
// what the body reads, so cast the props through `unknown` at the call site.
const Node = CustomNode as unknown as React.ComponentType<Record<string, unknown>>;

function renderNode(data: Record<string, unknown>, id = 'node_1') {
  return render(
    <I18nextProvider i18n={testI18n}>
      <Node data={data} id={id} />
    </I18nextProvider>,
  );
}

describe('CustomNode — output preview wiring for every node type', () => {
  beforeEach(() => useExecStreamStore.getState().reset());

  it.each(Object.keys(NODE_LABELS))('attaches a same-width output area to %s', (nodeType) => {
    const client = new QueryClient();
    client.setQueryData(['vfs', 'run-node-result', 'wf-preview', 'node_1'], {
      status: 'completed', output: { message: 'Preview wired' },
    });
    render(
      <QueryClientProvider client={client}>
        <I18nextProvider i18n={testI18n}>
          <MemoryRouter initialEntries={['/workflow/wf-preview']}>
            <Routes><Route path="/workflow/:wfId" element={<Node data={{ node_type: nodeType, node_name: 'test' }} id="node_1" />} /></Routes>
          </MemoryRouter>
        </I18nextProvider>
      </QueryClientProvider>,
    );
    const card = document.querySelector(`[aria-label="${nodeType} test"]`)!;
    const preview = document.querySelector('[data-node-output-preview="node_1"]')!;
    expect(card).toHaveClass('w-56');
    expect(preview).toHaveClass('w-56');
    expect(preview).toHaveTextContent('Preview wired');
    expect(preview.querySelector('[data-handle]')).toBeNull();
  });
});

describe('CustomNode — warning badge', () => {
  it('shows the ⚠ badge when __warnings__ is non-empty', () => {
    renderNode({
      node_type: 'LoopBeginNode',
      node_name: 'lb',
      __warnings__: ['canvas.warn.unpairedLoop'],
    });
    const badge = document.querySelector('[data-node-warning]');
    expect(badge).not.toBeNull();
  });

  it('hides the badge when there are no warnings', () => {
    renderNode({ node_type: 'CodeNode', node_name: 'c', __warnings__: [] });
    expect(document.querySelector('[data-node-warning]')).toBeNull();
  });

  it('hides the badge when __warnings__ is absent', () => {
    renderNode({ node_type: 'CodeNode', node_name: 'c' });
    expect(document.querySelector('[data-node-warning]')).toBeNull();
  });
});

describe('CustomNode — compact uniform card', () => {
  afterEach(() => cleanup());

  // The in/out summary is two stacked rows (Inputs row, Outputs row) — each a
  // `data-node-io` element whose textContent carries the count. No inline list.
  it('shows an in/out COUNT summary instead of listing each field inline', () => {
    renderNode({
      node_type: 'CodeNode',
      node_name: 'c',
      input_fields: { a: {}, b: {}, c: {} },
      output_fields: { x: {}, y: {} },
    });
    const inputs = document.querySelector('[data-node-io="inputs"]')!;
    const outputs = document.querySelector('[data-node-io="outputs"]')!;
    expect(inputs.textContent).toContain('3');
    expect(outputs.textContent).toContain('2');
    // The old inline `-> fieldname` list must be gone (constant-height card).
    expect(document.body.textContent).not.toContain('-> a');
    expect(document.body.textContent).not.toContain('->a');
  });

  it('keeps a constant width regardless of field count (uniform sizing)', () => {
    renderNode({ node_type: 'CodeNode', node_name: 'few', input_fields: { a: {} } });
    renderNode(
      {
        node_type: 'CodeNode',
        node_name: 'many',
        input_fields: { a: {}, b: {}, c: {}, d: {}, e: {} },
      },
      'node_2',
    );
    const few = document.querySelector('[aria-label="CodeNode few"]')!;
    const many = document.querySelector('[aria-label="CodeNode many"]')!;
    // Both cards carry the same fixed-width token → identical footprint.
    expect(few.className).toContain('w-56');
    expect(many.className).toContain('w-56');
  });

  it('zero fields still renders the summary (Inputs 0 · Outputs 0)', () => {
    renderNode({ node_type: 'CodeNode', node_name: 'c' });
    expect(
      document.querySelector('[data-node-io="inputs"]')!.textContent,
    ).toContain('0');
    expect(
      document.querySelector('[data-node-io="outputs"]')!.textContent,
    ).toContain('0');
  });

  it('renders a per-type header icon (svg glyph) next to the node name', () => {
    renderNode({ node_type: 'CodeNode', node_name: 'c' });
    const card = document.querySelector('[aria-label="CodeNode c"]')!;
    // lucide icons render as <svg>; the header carries one.
    expect(card.querySelector('svg')).not.toBeNull();
  });

  it('uses the node type color across the header instead of a gray header', () => {
    renderNode({ node_type: 'CodeNode', node_name: 'code' });
    const codeHeader = document.querySelector('[aria-label="CodeNode code"] [data-node-header]') as HTMLElement;
    expect(codeHeader.style.backgroundColor).not.toBe('');

    cleanup();
    renderNode({ node_type: 'ConditionNode', node_name: 'condition' });
    const conditionHeader = document.querySelector('[aria-label="ConditionNode condition"] [data-node-header]') as HTMLElement;
    expect(conditionHeader.style.backgroundColor).not.toBe(codeHeader.style.backgroundColor);
  });

  // The header title stack shows the node_id as a small gray subtitle UNDER the
  // node_name so the canonical id is visible without opening the Inspector.
  it('shows the node_id as a subtitle under the node_name', () => {
    renderNode({ node_type: 'CodeNode', node_name: 'c', node_id: 'node_3' }, 'node_3');
    const subtitle = document.querySelector('[data-node-id]');
    expect(subtitle).not.toBeNull();
    expect(subtitle!.textContent).toBe('node_3');
  });
});

describe('CustomNode — handle discoverability', () => {
  // Pure check (no render / no mock dependency): the shared handle class string
  // carries the discoverability affordance tokens.
  it('HANDLE_CLASS carries enlarged hit-area + crosshair + grow-on-hover', () => {
    expect(HANDLE_CLASS).toContain('cursor-crosshair');
    expect(HANDLE_CLASS).toContain('group-hover:');
    expect(HANDLE_CLASS).toContain('after:'); // enlarged transparent hit-area pad
  });

  it('source + target handles carry the affordance class', () => {
    renderNode({ node_type: 'CodeNode', node_name: 'c' });
    const handles = Array.from(document.querySelectorAll('[data-handle]'));
    expect(handles).toHaveLength(2); // target + source (CodeNode is neither Start nor End)
    for (const h of handles) {
      const cls = h.getAttribute('class') ?? '';
      expect(cls).toContain('cursor-crosshair');
      expect(cls).toContain('group-hover:');
    }
  });

  it('StartNode has only a source handle (no target)', () => {
    renderNode({ node_type: 'StartNode', node_name: 's' });
    expect(document.querySelector('[data-handle="target"]')).toBeNull();
    expect(document.querySelector('[data-handle="source"]')).not.toBeNull();
  });

  it('LoopEnd renders a non-connectable loop-back-source bottom handle', () => {
    renderNode({ node_type: 'LoopEndNode', node_name: 'le' });
    const back = document.querySelector('[data-handle-id="loop-back-source"]');
    expect(back).not.toBeNull();
    expect(back!.getAttribute('data-connectable')).toBe('false');
    expect(back!.getAttribute('data-handle')).toBe('source');
  });

  it('LoopBegin renders a non-connectable loop-back-target bottom handle', () => {
    renderNode({ node_type: 'LoopBeginNode', node_name: 'lb' });
    const back = document.querySelector('[data-handle-id="loop-back-target"]');
    expect(back).not.toBeNull();
    expect(back!.getAttribute('data-connectable')).toBe('false');
    expect(back!.getAttribute('data-handle')).toBe('target');
  });

  it('non-loop nodes render NO loop-back handle', () => {
    renderNode({ node_type: 'CodeNode', node_name: 'c' });
    expect(document.querySelector('[data-handle-id="loop-back-source"]')).toBeNull();
    expect(document.querySelector('[data-handle-id="loop-back-target"]')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Execution ring (folded in; see file header for the isolate:false
// rationale on why this can't be its own sibling file).
// ---------------------------------------------------------------------------
const applyExec = (u: Record<string, unknown>) =>
  act(() => useExecStreamStore.getState().applyUpdate(u));

const execCard = () =>
  document.querySelector('[aria-label="CodeNode c"]') as HTMLElement;

describe('CustomNode — execution ring', () => {
  beforeEach(() => act(() => useExecStreamStore.getState().reset()));
  afterEach(() => cleanup());

  it('shows a breathing halo + spinner when the node is running', () => {
    renderNode({ node_type: 'CodeNode', node_name: 'c' });
    applyExec({ node_id: 'node_1', status: 'running' });

    expect(execCard().getAttribute('data-exec-state')).toBe('running');
    // The calm "breathing" glow replaces the harsh animate-pulse blink.
    expect(execCard().className).toContain('animate-node-breathe');
    expect(execCard().className).not.toContain('animate-pulse');
    expect(document.querySelector('[data-exec-indicator="running"]')).not.toBeNull();
  });

  it('parallel running nodes each carry the breathing halo independently', () => {
    // Two cards, two distinct node ids both in `running`: each must light its
    // own breathing halo (no shared/global animation gate). Render two cards
    // with distinct ids and assert both breathe simultaneously.
    const { container: c1 } = renderNode(
      { node_type: 'CodeNode', node_name: 'b1' },
      'node_1',
    );
    const { container: c2 } = renderNode(
      { node_type: 'CodeNode', node_name: 'b2' },
      'node_2',
    );
    applyExec({ node_id: 'node_1', status: 'running' });
    applyExec({ node_id: 'node_2', status: 'running' });

    const card1 = c1.querySelector('[aria-label="CodeNode b1"]')!;
    const card2 = c2.querySelector('[aria-label="CodeNode b2"]')!;
    expect(card1.className).toContain('animate-node-breathe');
    expect(card2.className).toContain('animate-node-breathe');
  });

  it('shows a completed ring + check when the node completes', () => {
    renderNode({ node_type: 'CodeNode', node_name: 'c' });
    applyExec({ node_id: 'node_1', status: 'completed', result: 'ok' });

    expect(execCard().getAttribute('data-exec-state')).toBe('completed');
    expect(execCard().className).toContain('border-state-success');
    expect(document.querySelector('[data-exec-indicator="completed"]')).not.toBeNull();
  });

  it('shows an error ring and surfaces the node error on the node', () => {
    renderNode({ node_type: 'CodeNode', node_name: 'c' });
    applyExec({ node_id: 'node_1', status: 'running' });
    applyExec({ node_id: 'node_1', status: 'error', error: 'kaboom' });

    expect(execCard().getAttribute('data-exec-state')).toBe('error');
    expect(execCard().className).toContain('border-state-danger');
    const indicator = document.querySelector('[data-exec-indicator="error"]');
    expect(indicator).not.toBeNull();
    // The error message is surfaced on the node (the tooltip slot pattern).
    expect(indicator!.getAttribute('data-exec-error')).toBe('kaboom');
  });

  it('renders no exec ring/indicator when this node has no frame (idle)', () => {
    renderNode({ node_type: 'CodeNode', node_name: 'c' });
    // A frame for ANOTHER node must not light this one.
    applyExec({ node_id: 'node_2', status: 'running' });

    expect(execCard().getAttribute('data-exec-state')).toBeNull();
    expect(execCard().className).not.toContain('ring-blue-500');
    expect(document.querySelector('[data-exec-indicator]')).toBeNull();
  });

  it('reset() clears the ring (wfId-change / unmount path)', () => {
    renderNode({ node_type: 'CodeNode', node_name: 'c' });
    applyExec({ node_id: 'node_1', status: 'completed' });
    expect(execCard().getAttribute('data-exec-state')).toBe('completed');

    act(() => useExecStreamStore.getState().reset());

    expect(execCard().getAttribute('data-exec-state')).toBeNull();
    expect(execCard().className).not.toContain('ring-green-500');
    expect(document.querySelector('[data-exec-indicator]')).toBeNull();
  });

  it('execution ring is distinct from the ⚠ warning badge', () => {
    renderNode({
      node_type: 'CodeNode',
      node_name: 'c',
      __warnings__: ['canvas.warn.unpairedLoop'],
    });
    applyExec({ node_id: 'node_1', status: 'running' });

    // Both can coexist and are separate DOM elements.
    expect(document.querySelector('[data-node-warning]')).not.toBeNull();
    expect(document.querySelector('[data-exec-indicator="running"]')).not.toBeNull();
  });
});

describe('CustomNode — per-node selector scoping', () => {
  beforeEach(() => act(() => useExecStreamStore.getState().reset()));
  afterEach(() => cleanup());

  it('only the changed node re-renders when a sibling transitions', () => {
    // Count commits per node via React's Profiler — `onRender` fires only when
    // that subtree actually re-renders. CustomNode is memo'd and subscribes via
    // a PER-NODE selector, so a node_2 transition must NOT re-commit node_1.
    const commits = { node_1: 0, node_2: 0 };
    const onRender = (id: string) => {
      commits[id as 'node_1' | 'node_2'] += 1;
    };

    render(
      <I18nextProvider i18n={testI18n}>
        <Profiler id="node_1" onRender={onRender}>
          <Node data={{ node_type: 'CodeNode', node_name: 'a' }} id="node_1" />
        </Profiler>
        <Profiler id="node_2" onRender={onRender}>
          <Node data={{ node_type: 'CodeNode', node_name: 'b' }} id="node_2" />
        </Profiler>
      </I18nextProvider>,
    );

    const base1 = commits.node_1;
    const base2 = commits.node_2;

    // Transition ONLY node_2.
    applyExec({ node_id: 'node_2', status: 'running' });

    // node_1's selector output is unchanged (still undefined) → no re-commit;
    // node_2's status changed → re-commit.
    expect(commits.node_1).toBe(base1);
    expect(commits.node_2).toBeGreaterThan(base2);
  });
});

// ---------------------------------------------------------------------------
// Hover-card folding. The card's own rendering is tested in
// node-hover-card.test.tsx. Here we assert CustomNode FOLDED AWAY the two old
// Radix tooltips (the always-visible ⚠ + exec-error markers stay, but their
// per-icon tooltip is gone — the detail moved into the hover card). The
// node body renders as the hover-card trigger child, so the markers are still
// in the DOM. We deliberately do NOT module-mock NodeHoverCard here: under
// vitest isolate:false a per-file mock of a module other files import for real
// clobbers the shared graph (feedback_vitest_isolate_false). The suppression
// WIRING is covered by the pure `nodeHoverSuppressed` unit tests below instead.
// ---------------------------------------------------------------------------
describe('CustomNode — folded tooltips', () => {
  beforeEach(() => act(() => useExecStreamStore.getState().reset()));
  afterEach(() => cleanup());

  it('the warning ⚠ badge stays, but its old per-icon Radix tooltip is GONE', () => {
    renderNode({
      node_type: 'LoopBeginNode',
      node_name: 'lb',
      __warnings__: ['canvas.warn.unpairedLoop'],
    });
    // Always-visible marker remains as a plain span (no TooltipTrigger).
    const badge = document.querySelector('[data-node-warning]')!;
    expect(badge).not.toBeNull();
    expect(badge.getAttribute('data-state')).toBeNull(); // not a Radix trigger
    // No Radix tooltip content rendered (it was folded into the hover card).
    expect(document.querySelector('[role="tooltip"]')).toBeNull();
  });

  it('the exec-error indicator stays, but its old per-icon Radix tooltip is GONE', () => {
    renderNode({ node_type: 'CodeNode', node_name: 'c' });
    applyExec({ node_id: 'node_1', status: 'error', error: 'kaboom' });
    const indicator = document.querySelector('[data-exec-indicator="error"]')!;
    expect(indicator).not.toBeNull();
    // The error is still surfaced through the node attribute
    // contract) but no per-icon tooltip wraps it anymore.
    expect(indicator.getAttribute('data-exec-error')).toBe('kaboom');
    expect(indicator.getAttribute('data-state')).toBeNull();
    expect(document.querySelector('[role="tooltip"]')).toBeNull();
  });
});

// Pure suppression-rule unit tests: no rendering or mocks.
describe('nodeHoverSuppressed', () => {
  it('NOT suppressed when idle + unselected', () => {
    expect(
      nodeHoverSuppressed({
        canvasInteracting: false,
        selected: false,
        inspectorScope: 'auto',
      }),
    ).toBe(false);
  });

  it('suppressed while the canvas is interacting (drag/connect)', () => {
    expect(
      nodeHoverSuppressed({
        canvasInteracting: true,
        selected: false,
        inspectorScope: 'auto',
      }),
    ).toBe(true);
  });

  it('suppressed when THIS node is open in the Inspector (selected + node scope)', () => {
    expect(
      nodeHoverSuppressed({
        canvasInteracting: false,
        selected: true,
        inspectorScope: 'auto',
      }),
    ).toBe(true);
  });

  it('NOT suppressed when selected but the scope is the workflow override', () => {
    expect(
      nodeHoverSuppressed({
        canvasInteracting: false,
        selected: true,
        inspectorScope: 'workflow',
      }),
    ).toBe(false);
  });
});
