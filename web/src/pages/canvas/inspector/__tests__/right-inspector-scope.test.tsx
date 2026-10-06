/**
 * RightInspector contextual scope-driven tab sets.
 *
 *   - NODE scope (a node selected, scope auto): Node / Run node / Info.
 *   - WORKFLOW scope (no selection OR scope==='workflow'): Run / Batch.
 *   - run-start (idle→running edge) auto-focuses the workflow Run tab.
 *   - requestCheck() opens the Check dialog.
 *
 * Tab bodies are mocked because this suite asserts only the tab strip.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, cleanup, act } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import i18n from 'i18next';

const selection = { nodes: [] as { id: string; selected: boolean; data: unknown }[] };
vi.mock('@xyflow/react', () => ({
  useNodes: () => selection.nodes,
}));

// Mock the tab bodies + the Check dialog so we test only RightInspector's strip.
vi.mock('@/pages/canvas/inspector/NodeTab', () => ({
  NodeTab: () => <div data-testid="body-node" />,
}));
vi.mock('@/pages/canvas/inspector/NodeExecutePanel', () => ({
  NodeExecutePanel: () => <div data-testid="body-run-node" />,
}));
vi.mock('@/pages/canvas/inspector/InfoTab', () => ({
  InfoTab: () => <div data-testid="body-info" />,
}));
vi.mock('@/pages/canvas/inspector/WorkflowRunTab', () => ({
  WorkflowRunTab: () => <div data-testid="body-run" />,
}));
vi.mock('@/pages/canvas/inspector/BatchTab', () => ({
  BatchTab: () => <div data-testid="body-batch" />,
}));
const checkDialogSpy = vi.fn();
vi.mock('@/components/modals/WorkflowCheckDialog', () => ({
  WorkflowCheckDialog: ({ open }: { open: boolean }) => {
    checkDialogSpy(open);
    return open ? <div data-testid="check-dialog" /> : null;
  },
}));

import { RightInspector } from '@/pages/canvas/inspector/RightInspector';
import { useUIStore } from '@/stores/ui';
import { useExecStreamStore } from '@/stores/exec-stream';

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: {} } },
  interpolation: { escapeValue: false },
});

function renderInspector() {
  return render(
    <I18nextProvider i18n={testI18n}>
      <RightInspector wfId="wf-1" canExecute />
    </I18nextProvider>,
  );
}

beforeEach(() => {
  selection.nodes = [];
  checkDialogSpy.mockClear();
  useExecStreamStore.getState().reset();
  useUIStore.setState({
    inspectorScope: 'auto',
    inspectorTab: 'node',
    checkRequestId: 0,
  });
});
afterEach(() => cleanup());

describe('RightInspector scope-driven tab sets', () => {
  it('NODE scope (node selected) shows Node / Run node / Info', () => {
    selection.nodes = [{ id: 'node_1', selected: true, data: {} }];
    renderInspector();
    expect(screen.getByTestId('inspector-tab-node')).toBeInTheDocument();
    expect(screen.getByTestId('inspector-tab-run-node')).toBeInTheDocument();
    expect(screen.getByTestId('inspector-tab-info')).toBeInTheDocument();
    // No workflow-scope tabs.
    expect(screen.queryByTestId('inspector-tab-run')).toBeNull();
    expect(screen.queryByTestId('inspector-tab-batch')).toBeNull();
    expect(screen.getAllByRole('tab')).toHaveLength(3);
  });

  it('WORKFLOW scope (no selection) shows Run / Batch / Schedule', () => {
    selection.nodes = [];
    renderInspector();
    expect(screen.getByTestId('inspector-tab-run')).toBeInTheDocument();
    expect(screen.getByTestId('inspector-tab-batch')).toBeInTheDocument();
    expect(screen.queryByTestId('inspector-tab-node')).toBeNull();
    // The standalone Execution tab is GONE.
    expect(screen.queryByTestId('inspector-tab-execution')).toBeNull();
    expect(screen.getByTestId('inspector-tab-schedule')).toBeInTheDocument();
    expect(screen.getAllByRole('tab')).toHaveLength(3);
  });

  it('explicit workflow override (no node selected) shows workflow tabs', () => {
    selection.nodes = [];
    useUIStore.setState({ inspectorScope: 'workflow', inspectorTab: 'run' });
    renderInspector();
    expect(screen.getByTestId('inspector-tab-run')).toBeInTheDocument();
    expect(screen.queryByTestId('inspector-tab-node')).toBeNull();
  });

  it('selecting a node clears a stale workflow override → node scope (auto)', async () => {
    // A node is selected WHILE a workflow override is still set (e.g. a race
    // before the toolbar deselect). The effect resolves it back to auto.
    selection.nodes = [{ id: 'node_1', selected: true, data: {} }];
    useUIStore.setState({ inspectorScope: 'workflow', inspectorTab: 'run' });
    renderInspector();
    // The reconciling effect flips scope back to auto.
    expect(useUIStore.getState().inspectorScope).toBe('auto');
    expect(await screen.findByTestId('inspector-tab-node')).toBeInTheDocument();
  });

  it('run-start (idle→running) requests the workflow Run tab', () => {
    renderInspector();
    act(() => {
      useExecStreamStore.getState().setStatus('running');
    });
    const s = useUIStore.getState();
    expect(s.inspectorScope).toBe('workflow');
    expect(s.inspectorTab).toBe('run');
  });

  it('root container is text-selectable (select-text) so titles/hints/errors are copyable', () => {
    selection.nodes = [];
    renderInspector();
    // The sider root carries `select-text`, overriding any inherited
    // `user-select: none` so all descendant text can be selected + Ctrl+C copied.
    expect(document.querySelector('.select-text')).toBeInTheDocument();
  });

  it('requestCheck opens the Check dialog', () => {
    renderInspector();
    expect(screen.queryByTestId('check-dialog')).toBeNull();
    act(() => {
      useUIStore.getState().requestCheck();
    });
    expect(screen.getByTestId('check-dialog')).toBeInTheDocument();
  });
});
