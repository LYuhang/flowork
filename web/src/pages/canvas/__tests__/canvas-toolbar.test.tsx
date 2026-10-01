/**
 * CanvasToolbar bundle covering Inspector navigation, JSON I/O, and auto-layout.
 *
 * Behavior asserted here:
 *   - Execute / Run Batch no longer open modals — they `requestInspectorTab`
 *     ('workflow','run'|'batch') and deselect the xyflow node.
 *   - Check lives in the More menu and fires `requestCheck()`.
 *
 * The toolbar now reads `useReactFlow().setNodes` (deselect), so it must be
 * wrapped in a `ReactFlowProvider`. The network seams (`streamExecution`,
 * `useWorkflow`, `useCommitWorkflow`, `sonner`) are mocked; the store
 * (`useWorkflowEditStore` / `useUIStore`) and the IO/layout helpers run real.
 *
 * The ⋯-More menu is a Radix DropdownMenu — opened via `userEvent`. Items are
 * selected by `data-action` (the stable palette contract).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { ReactFlowProvider } from '@xyflow/react';
import i18n from 'i18next';

const { toastSpy, useWorkflowMock, commitMock, commitTargetMajorSpy, newMajorMock, navigateSpy } =
  vi.hoisted(() => ({
    toastSpy: { success: vi.fn(), error: vi.fn() },
    useWorkflowMock: vi.fn((..._a: unknown[]) => ({
      data: { meta: { workflow_name: 'My Flow', active_v: 2, active_sv: 1 } },
    })),
    // mutate(draft, opts) — record the draft; success echoes a meta so the
    // pinned-Save navigation can run.
    commitMock: { mutate: vi.fn(), mutateAsync: vi.fn(async () => ({})), isPending: false },
    // Captures the `target_major` the toolbar binds into useCommitWorkflow.
    commitTargetMajorSpy: vi.fn(),
    newMajorMock: { mutate: vi.fn(), mutateAsync: vi.fn(async () => ({})), isPending: false },
    navigateSpy: vi.fn(),
  }));

vi.mock('sonner', () => ({ toast: toastSpy }));
vi.mock('react-router', () => ({ useNavigate: () => navigateSpy }));
vi.mock('@/lib/api/queries/workflow', () => ({
  useWorkflow: (...a: unknown[]) => useWorkflowMock(...a),
}));
vi.mock('@/lib/api/mutations/workflow-ops', () => ({
  useCommitWorkflow: (_wfId: string, targetMajor?: number | null) => {
    commitTargetMajorSpy(targetMajor);
    return commitMock;
  },
  useNewMajorVersion: () => newMajorMock,
}));
vi.mock('@/lib/api/queries/workflow-sandbox', () => ({
  useWorkflowSandboxStatus: () => ({
    data: { status: 'idle', active_execution_ids: [] },
  }),
  useStartWorkflowSandbox: () => ({ mutate: vi.fn(), isPending: false }),
  useCloseWorkflowSandbox: () => ({ mutate: vi.fn(), isPending: false }),
}));
// The toolbar always mounts WorkflowSettingsModal, which calls useEnums()
// (TanStack Query) on mount. Stub the WRAPPING component (not the shared
// `enums` query) so we don't clobber sibling tests under isolate=false — the
// toolbar's job is only to render the gear + open it; the modal's own behaviour
// is covered in workflow-settings-modal.test.tsx. It echoes `open` as a testid
// so the open-on-click test can observe it.
vi.mock('@/pages/canvas/WorkflowSettingsModal', () => ({
  WorkflowSettingsModal: ({ open }: { open: boolean }) =>
    open ? <div data-testid="workflow-settings-modal" /> : null,
}));

import { ExecutionHistoryContext, type ExecutionHistoryValue } from '../ExecutionHistoryContext';
import { CanvasToolbar } from '@/pages/canvas/CanvasToolbar';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import { useExecStreamStore } from '@/stores/exec-stream';
import { useUIStore } from '@/stores/ui';

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: {} } },
  interpolation: { escapeValue: false },
});

function renderToolbar(readOnly = false, pinnedMajor: number | null = null, history: ExecutionHistoryValue | null = null) {
  render(
    <I18nextProvider i18n={testI18n}>
      <ExecutionHistoryContext.Provider value={history}>
      <ReactFlowProvider>
        <CanvasToolbar
          wfId="wf-1"
          readOnly={readOnly}
          canExecute
          canExport
          canMount
          canInspectRuns
          canCancel
          pinnedMajor={pinnedMajor}
          onToggleExplorer={vi.fn()}
          explorerOpen={false}
        />
      </ReactFlowProvider>
      </ExecutionHistoryContext.Provider>
    </I18nextProvider>,
  );
}

const startNode = (fields: Record<string, unknown> = {}) => ({
  node_id: 'node_1',
  node_name: 'start',
  node_type: 'StartNode',
  input_fields: fields,
  children: [],
});

const byAction = (action: string) =>
  document.querySelector(`[data-action="${action}"]`) as HTMLElement;

async function openMore() {
  const user = userEvent.setup();
  await user.click(byAction('canvas-more'));
  await screen.findByRole('menu');
  return user;
}

beforeEach(() => {
  toastSpy.success.mockClear();
  toastSpy.error.mockClear();
  commitMock.mutate.mockClear();
  commitMock.mutateAsync.mockClear();
  commitTargetMajorSpy.mockClear();
  newMajorMock.mutateAsync.mockClear();
  navigateSpy.mockClear();
  useExecStreamStore.getState().reset();
  useUIStore.setState({
    inspectorScope: 'auto',
    inspectorTab: 'node',
    checkRequestId: 0,
  });
});

describe('Execute and Run Batch open the Inspector', () => {
  it('Execute focuses the workflow Run tab (no execute modal)', () => {
    useWorkflowEditStore.getState().setDraft({ node_1: startNode(), __meta__: {} });
    renderToolbar();
    fireEvent.click(byAction('execute'));
    const s = useUIStore.getState();
    expect(s.inspectorScope).toBe('workflow');
    expect(s.inspectorTab).toBe('run');
    // No dialog, and the toolbar no longer mounts the retired execute modal.
    expect(screen.queryByTestId('execute-input-dialog')).toBeNull();
  });

  it('Run Batch focuses the workflow Batch tab (no batch modal)', () => {
    useWorkflowEditStore.getState().setDraft({ node_1: startNode(), __meta__: {} });
    renderToolbar();
    fireEvent.click(byAction('canvas-run-batch'));
    const s = useUIStore.getState();
    expect(s.inspectorScope).toBe('workflow');
    expect(s.inspectorTab).toBe('batch');
  });
});

describe('Check is in the More menu and fires requestCheck', () => {
  it('the standalone Check toolbar button is gone', () => {
    useWorkflowEditStore.getState().setDraft({ node_1: startNode(), __meta__: {} });
    renderToolbar();
    // No top-level button — only the menu item (which is unmounted until open).
    expect(byAction('check')).toBeNull();
  });

  it('the ⋯ Check item bumps checkRequestId', async () => {
    useWorkflowEditStore.getState().setDraft({ node_1: startNode(), __meta__: {} });
    renderToolbar();
    expect(useUIStore.getState().checkRequestId).toBe(0);
    await openMore();
    fireEvent.click(byAction('check'));
    await waitFor(() => expect(useUIStore.getState().checkRequestId).toBe(1));
  });
});

describe('JSON download (Stream 6)', () => {
  it('produces a blob containing the serialized workflow', async () => {
    const draft = { node_1: startNode(), __meta__: { workflow_name: 'My Flow' } };
    useWorkflowEditStore.getState().setDraft(draft);

    let blobText = '';
    const realCreate = URL.createObjectURL;
    const realRevoke = URL.revokeObjectURL;
    let created = false;
    (URL as unknown as { createObjectURL: (b: Blob) => string }).createObjectURL = (b: Blob) => {
      created = true;
      void b.text().then((tx) => (blobText = tx));
      return 'blob:mock';
    };
    (URL as unknown as { revokeObjectURL: (u: string) => void }).revokeObjectURL = vi.fn();
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {});

    renderToolbar();
    await openMore();
    fireEvent.click(byAction('wf-download'));

    expect(created).toBe(true);
    expect(clickSpy).toHaveBeenCalled();
    await waitFor(() => expect(JSON.parse(blobText)).toMatchObject(draft));

    clickSpy.mockRestore();
    (URL as unknown as { createObjectURL: typeof realCreate }).createObjectURL = realCreate;
    (URL as unknown as { revokeObjectURL: typeof realRevoke }).revokeObjectURL = realRevoke;
  });
});

describe('JSON upload (Stream 6)', () => {
  function uploadFile(json: string) {
    const file = new File([json], 'wf.json', { type: 'application/json' });
    Object.defineProperty(file, 'text', { value: () => Promise.resolve(json) });
    const input = screen.getByTestId('wf-upload-input') as HTMLInputElement;
    Object.defineProperty(input, 'files', { value: [file], configurable: true });
    fireEvent.change(input);
  }

  it('preserves meta, loads via applyEdit-replace (dirty + undoable) + auto-layouts position-less nodes', async () => {
    useWorkflowEditStore.getState().setDraft({ __meta__: { workflow_name: 'Keep Me' } });
    renderToolbar();

    const uploaded = JSON.stringify({
      node_1: { node_id: 'node_1', node_type: 'StartNode', node_name: 's', children: ['node_2'] },
      node_2: { node_id: 'node_2', node_type: 'EndNode', node_name: 'e', children: [] },
      __meta__: {
        workflow_name: 'Imported',
        active_v: 99,
        settings: { code_requirements: 'pandas==2.2.0' },
      },
      wf_id: 'other',
    });
    uploadFile(uploaded);

    await waitFor(() => {
      const draft = useWorkflowEditStore.getState().draft!;
      expect(draft.node_1).toBeDefined();
      expect(draft.node_2).toBeDefined();
    });
    const state = useWorkflowEditStore.getState();
    const draft = state.draft!;
    expect(draft.__meta__).toMatchObject({
      workflow_name: 'Imported',
      active_v: 99,
      settings: { code_requirements: 'pandas==2.2.0' },
    });
    expect(draft.wf_id).toBeUndefined();
    expect(state.dirty).toBe(true);
    expect(state.undoStack.length).toBe(1);
    const attrs = (draft.node_1 as { __attributes__: { x: number; y: number } }).__attributes__;
    expect(typeof attrs.x).toBe('number');
    expect(typeof attrs.y).toBe('number');
    state.undo();
    expect(useWorkflowEditStore.getState().draft!.node_1).toBeUndefined();
  });

  it('toasts + does not mutate on malformed JSON', async () => {
    useWorkflowEditStore.getState().setDraft({ __meta__: {} });
    renderToolbar();
    uploadFile('{ this is not json');
    await waitFor(() => expect(toastSpy.error).toHaveBeenCalled());
    expect(useWorkflowEditStore.getState().dirty).toBe(false);
  });

  it('shows a confirm dialog when the canvas already has nodes', async () => {
    useWorkflowEditStore.getState().setDraft({ node_1: startNode(), __meta__: {} });
    renderToolbar();
    uploadFile(
      JSON.stringify({ node_5: { node_id: 'node_5', node_type: 'EndNode', children: [] } }),
    );
    await screen.findByTestId('upload-confirm-dialog');
    expect(useWorkflowEditStore.getState().draft!.node_5).toBeUndefined();
    fireEvent.click(byAction('wf-upload-confirm'));
    await waitFor(() =>
      expect(useWorkflowEditStore.getState().draft!.node_5).toBeDefined(),
    );
    expect(useWorkflowEditStore.getState().draft!.node_1).toBeUndefined();
  });
});

describe('Auto-layout (Stream 8 / N1)', () => {
  it('repositions nodes in one undo step', async () => {
    useWorkflowEditStore.getState().setDraft({
      node_1: {
        node_id: 'node_1',
        node_type: 'StartNode',
        children: ['node_2'],
        __attributes__: { x: 0, y: 0 },
      },
      node_2: {
        node_id: 'node_2',
        node_type: 'EndNode',
        children: [],
        __attributes__: { x: 0, y: 0 },
      },
      __meta__: {},
    });
    renderToolbar();
    await openMore();
    fireEvent.click(byAction('canvas-auto-layout'));

    const state = useWorkflowEditStore.getState();
    expect(state.undoStack.length).toBe(1);
    const n1 = (state.draft!.node_1 as { __attributes__: { x: number; y: number } })
      .__attributes__;
    const n2 = (state.draft!.node_2 as { __attributes__: { x: number; y: number } })
      .__attributes__;
    expect(n2.x).toBeGreaterThan(n1.x);
  });
});

describe('serialize runs', () => {
  it('keeps run ownership in the inspector while a workflow run is active', () => {
    useWorkflowEditStore.getState().setDraft({ node_1: startNode(), __meta__: {} });
    const { rerender } = render(
      <I18nextProvider i18n={testI18n}>
        <ReactFlowProvider>
          <CanvasToolbar
            wfId="wf-1"
            readOnly={false}
            canExecute
            canExport
            canMount
            canInspectRuns
            canCancel
            onToggleExplorer={vi.fn()}
            explorerOpen={false}
          />
        </ReactFlowProvider>
      </I18nextProvider>,
    );
    expect(byAction('execute')).not.toBeNull();
    expect(byAction('cancel')).toBeNull();

    act(() => useExecStreamStore.setState({ wfId: 'wf-1', status: 'running' }));
    rerender(
      <I18nextProvider i18n={testI18n}>
        <ReactFlowProvider>
          <CanvasToolbar
            wfId="wf-1"
            readOnly={false}
            canExecute
            canExport
            canMount
            canInspectRuns
            canCancel
            onToggleExplorer={vi.fn()}
            explorerOpen={false}
          />
        </ReactFlowProvider>
      </I18nextProvider>,
    );

    expect(byAction('cancel')).toBeNull();
    const execute = byAction('execute') as HTMLButtonElement;
    expect(execute).not.toBeNull();
    expect(execute.disabled).toBe(true);
  });
});

describe('edit freeze', () => {
  it('does not render a toolbar Cancel while readOnly; cancellation lives in the Run tab', () => {
    useWorkflowEditStore.getState().setDraft({ node_1: startNode(), __meta__: {} });
    act(() => useExecStreamStore.setState({ wfId: 'wf-1', status: 'running' }));
    renderToolbar(true);
    expect(byAction('cancel')).toBeNull();
    expect((byAction('execute') as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('readOnly', () => {
  it('disables the mutating ⋯ items', async () => {
    useWorkflowEditStore.getState().setDraft({ node_1: startNode(), __meta__: {} });
    renderToolbar(true);
    await openMore();
    expect(byAction('wf-upload')).toHaveAttribute('aria-disabled', 'true');
    expect(byAction('canvas-auto-layout')).toHaveAttribute('aria-disabled', 'true');
  });
});

describe('Workflow Settings gear (#484)', () => {
  it('renders the gear in manual (workflow) mode', () => {
    useWorkflowEditStore.getState().setDraft({ node_1: startNode(), __meta__: {} });
    renderToolbar();
    expect(byAction('canvas-settings')).not.toBeNull();
  });

  it('disables the gear when readOnly', () => {
    useWorkflowEditStore.getState().setDraft({ node_1: startNode(), __meta__: {} });
    renderToolbar(true);
    const gear = byAction('canvas-settings') as HTMLButtonElement;
    expect(gear).not.toBeNull();
    expect(gear.disabled).toBe(true);
  });

  it('opens the settings modal on click', async () => {
    useWorkflowEditStore.getState().setDraft({ node_1: startNode(), __meta__: {} });
    renderToolbar();
    fireEvent.click(byAction('canvas-settings'));
    await screen.findByTestId('workflow-settings-modal');
  });
});

describe('New version button (UX-5 Part A)', () => {
  it('saves then creates a new major version and navigates to the live workflow', async () => {
    useWorkflowEditStore.getState().setDraft({ node_1: startNode(), __meta__: {} });
    // Make the draft dirty so the save-then-new-major path saves first.
    useWorkflowEditStore.getState().applyEdit((wf) => {
      (wf.node_1 as { node_name: string }).node_name = 'edited';
      return wf;
    });
    renderToolbar();
    const btn = byAction('canvas-new-version');
    expect(btn).not.toBeNull();
    fireEvent.click(btn);
    await waitFor(() => expect(newMajorMock.mutateAsync).toHaveBeenCalled());
    // Dirty → saved first.
    expect(commitMock.mutateAsync).toHaveBeenCalled();
    expect(navigateSpy).toHaveBeenCalledWith('/workflow/wf-1');
  });

  it('is hidden on a pinned historical route', () => {
    useWorkflowEditStore.getState().setDraft({ node_1: startNode(), __meta__: {} });
    renderToolbar(false, 1);
    expect(byAction('canvas-new-version')).toBeNull();
  });

  it('is disabled when readOnly (run freeze)', () => {
    useWorkflowEditStore.getState().setDraft({ node_1: startNode(), __meta__: {} });
    renderToolbar(true);
    const btn = byAction('canvas-new-version') as HTMLButtonElement;
    expect(btn).not.toBeNull();
    expect(btn.disabled).toBe(true);
  });
});

describe('Editable historical version Save (UX-5 Part B)', () => {
  it('binds target_major into useCommitWorkflow when pinned', () => {
    useWorkflowEditStore.getState().setDraft({ node_1: startNode(), __meta__: {} });
    renderToolbar(false, 3);
    // The toolbar constructed the commit hook with the pinned major.
    expect(commitTargetMajorSpy).toHaveBeenCalledWith(3);
  });

  it('Save is ENABLED (not readOnly-disabled) on a pinned route and commits with a success navigation', () => {
    useWorkflowEditStore.getState().setDraft({ node_1: startNode(), __meta__: {} });
    // Dirty so Save isn't gated by `!dirty`.
    useWorkflowEditStore.getState().applyEdit((wf) => {
      (wf.node_1 as { node_name: string }).node_name = 'edited';
      return wf;
    });
    // Echo the just-saved (major, sub) so the pinned-Save navigation runs.
    commitMock.mutate.mockImplementationOnce(
      (_draft: unknown, opts?: { onSuccess?: (m: unknown) => void }) => {
        opts?.onSuccess?.({ active_v: 3, active_sv: 5 });
      },
    );
    renderToolbar(false, 3);
    const save = byAction('canvas-save') as HTMLButtonElement;
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    expect(commitMock.mutate).toHaveBeenCalled();
    expect(navigateSpy).toHaveBeenCalledWith('/workflow/wf-1/version/v3.sv5');
  });
});


it('disables Execute for a restored approval with no local stream', () => {
  useWorkflowEditStore.getState().setDraft({ node_1: startNode(), __meta__: {} });
  renderToolbar(false, null, {
    detail: { id: 'restored', wf_id: 'wf-1', status: 'waiting_approval' },
    latestNodeEvents: {},
  } as ExecutionHistoryValue);
  expect(byAction('execute')).toBeDisabled();
});
