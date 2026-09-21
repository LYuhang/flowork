/**
 * WorkflowRunTab contains assertions that used to
 * live on `ExecuteInputDialog.test.tsx` (input form + type coercion) and
 * `execution-tab.test.tsx` (the reload-safe persisted hydrate), now that both
 * the modal and the standalone Execution tab fold into this ONE inline tab.
 *
 * We mock:
 *   - `streamExecution` — assert Run ships raw editable input strings.
 *   - the executions REST client — drive the persisted hydrate without network.
 * The exec-stream store + the workflow-edit draft store run real.
 */
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { act } from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import i18n from 'i18next';

const { streamExecutionMock, getWorkflowExecutionStatus, readVfsRunMock } = vi.hoisted(
  () => ({
    streamExecutionMock: vi.fn((..._a: unknown[]) => Promise.resolve()),
    getWorkflowExecutionStatus: vi.fn(),
    readVfsRunMock: vi.fn(),
  }),
);

vi.mock('@/lib/api/sse/exec-stream', () => ({
  streamExecution: (...a: unknown[]) => streamExecutionMock(...a),
}));
vi.mock('@/lib/api/executions', () => ({
  getWorkflowExecutionStatus: (...a: unknown[]) => getWorkflowExecutionStatus(...a),
  cancelWorkflowExecution: vi.fn(() => Promise.resolve()),
}));
vi.mock('@/lib/api/vfs', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/vfs')>()),
  readVfsRun: (...a: unknown[]) => readVfsRunMock(...a),
}));

import { WorkflowRunTab } from '@/pages/canvas/inspector/WorkflowRunTab';
import { useExecStreamStore } from '@/stores/exec-stream';
import { useWorkflowEditStore } from '@/stores/workflow-edit';

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: {} } },
  interpolation: { escapeValue: false },
});

function startWith(inputFields: Record<string, unknown>) {
  return {
    node_1: {
      node_id: 'node_1',
      node_name: '__start__',
      node_type: 'StartNode',
      input_fields: inputFields,
      output_fields: {},
      node_config: {},
      children: [],
    },
    __meta__: {},
  };
}

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <I18nextProvider i18n={testI18n}>
        <WorkflowRunTab wfId="wf_1" />
      </I18nextProvider>
    </QueryClientProvider>,
  );
}

describe('WorkflowRunTab', () => {
  beforeEach(() => {
    streamExecutionMock.mockClear();
    getWorkflowExecutionStatus.mockReset();
    getWorkflowExecutionStatus.mockResolvedValue(null);
    readVfsRunMock.mockReset();
    readVfsRunMock.mockResolvedValue({
      path: '/run/__exec__/inputs.json',
      content_type: 'application/json',
      content: '{}',
      size_bytes: 2,
      truncated: false,
      wf_version: null,
      stale: false,
    });
    (globalThis as unknown as { __mockReadVfsRun?: typeof readVfsRunMock })
      .__mockReadVfsRun = readVfsRunMock;
    // Full reset between tests: `reset()` deliberately preserves the per-wfId
    // input buffer (persisting inputs across inspector re-entry is the feature),
    // but tests must start clean or one test's typed inputs seed the next.
    act(() => {
      useExecStreamStore.getState().reset();
      useExecStreamStore.setState({ inputsByWorkflow: {} });
    });
  });
  afterEach(() => {
    delete (globalThis as unknown as { __mockReadVfsRun?: typeof readVfsRunMock })
      .__mockReadVfsRun;
    cleanup();
  });

  it('renders one input row per StartNode field, no reference toggle', () => {
    useWorkflowEditStore.getState().setDraft(
      startWith({
        query: { type: 'string', value: '', reference: '' },
        limit: { type: 'number', value: '', reference: '' },
      }),
    );
    renderTab();
    expect(screen.getByTestId('exec-field-query')).toBeInTheDocument();
    expect(screen.getByTestId('exec-field-limit')).toBeInTheDocument();
    // allowReference=false → no reference toggle at the input boundary.
    expect(screen.queryByTestId('exec-input-query-ref-toggle')).toBeNull();
  });

  it('ships a number field as its raw string before streaming', async () => {
    useWorkflowEditStore.getState().setDraft(
      startWith({ limit: { type: 'number', value: '', reference: '' } }),
    );
    renderTab();
    const input = screen.getByTestId('exec-input-limit-input');
    fireEvent.change(input, { target: { value: '42' } });
    fireEvent.blur(input);
    fireEvent.click(screen.getByTestId('workflow-run-tab').querySelector('[data-action="run-workflow"]')!);

    await waitFor(() => expect(streamExecutionMock).toHaveBeenCalledTimes(1));
    const arg = streamExecutionMock.mock.calls[0][0] as {
      wfId: string;
      input: Record<string, unknown>;
    };
    expect(arg.wfId).toBe('wf_1');
    expect(arg.input.limit).toBe('42');
    expect(typeof arg.input.limit).toBe('string');
  });

  it('ships an untouched boolean switch as false', async () => {
    useWorkflowEditStore.getState().setDraft(
      startWith({ enabled: { type: 'boolean', value: '', reference: '' } }),
    );
    renderTab();

    fireEvent.click(
      screen.getByTestId('workflow-run-tab').querySelector('[data-action="run-workflow"]')!,
    );

    await waitFor(() => expect(streamExecutionMock).toHaveBeenCalledTimes(1));
    const arg = streamExecutionMock.mock.calls[0][0] as {
      input: Record<string, unknown>;
    };
    expect(arg.input.enabled).toBe(false);
  });

  it('normalizes a legacy-persisted empty boolean buffer to false', async () => {
    useWorkflowEditStore.getState().setDraft(
      startWith({ enabled: { type: 'boolean', value: '', reference: '' } }),
    );
    useExecStreamStore.getState().setWorkflowInputs('wf_1', { enabled: '' });
    renderTab();

    fireEvent.click(
      screen.getByTestId('workflow-run-tab').querySelector('[data-action="run-workflow"]')!,
    );

    await waitFor(() => expect(streamExecutionMock).toHaveBeenCalledTimes(1));
    const arg = streamExecutionMock.mock.calls[0][0] as {
      input: Record<string, unknown>;
    };
    expect(arg.input.enabled).toBe(false);
  });

  it('ships list/object-like StartNode fields as raw strings before streaming', async () => {
    useWorkflowEditStore.getState().setDraft(
      startWith({
        image_urls: { type: 'list', value: '', reference: '' },
        meta: { type: 'dict', value: '', reference: '' },
      }),
    );
    renderTab();
    const urls = screen.getByTestId('exec-input-image_urls-json');
    const meta = screen.getByTestId('exec-input-meta-json');
    fireEvent.change(urls, { target: { value: '["u1","u2"]' } });
    fireEvent.blur(urls);
    fireEvent.change(meta, { target: { value: '{"source":"test"}' } });
    fireEvent.blur(meta);

    fireEvent.click(
      screen.getByTestId('workflow-run-tab').querySelector('[data-action="run-workflow"]')!,
    );

    await waitFor(() => expect(streamExecutionMock).toHaveBeenCalledTimes(1));
    const arg = streamExecutionMock.mock.calls[0][0] as {
      input: Record<string, unknown>;
    };
    expect(arg.input.image_urls).toBe('["u1","u2"]');
    expect(typeof arg.input.image_urls).toBe('string');
    expect(arg.input.meta).toBe('{"source":"test"}');
  });

  it('clears previous run output immediately after clicking Run', async () => {
    useWorkflowEditStore.getState().setDraft(startWith({}));
    act(() => {
      const s = useExecStreamStore.getState();
      s.begin('wf_1', new AbortController());
      s.applyUpdate({ node_id: 'node_1', status: 'completed', result: 'old' });
      s.setStatus('completed');
    });
    renderTab();
    expect(screen.getByText('old')).toBeTruthy();

    fireEvent.click(
      screen.getByTestId('workflow-run-tab').querySelector('[data-action="run-workflow"]')!,
    );

    await waitFor(() => expect(streamExecutionMock).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('old')).toBeNull();
    const s = useExecStreamStore.getState();
    expect(s.status).toBe('running');
    expect(s.perNode).toEqual({});
  });

  it('hides persisted previous output immediately after clicking Run', async () => {
    useWorkflowEditStore.getState().setDraft(startWith({}));
    getWorkflowExecutionStatus.mockResolvedValue({
      exec_id: 'exec_old',
      wf_id: 'wf_1',
      status: 'completed',
      started_at: 1000,
      finished_at: 1002,
      result: {
        node_1: {
          status: 'completed',
          execution_result: 'persisted-old',
        },
      },
      error: null,
    });
    renderTab();
    await waitFor(() => expect(screen.getByText('persisted-old')).toBeTruthy());

    fireEvent.click(
      screen.getByTestId('workflow-run-tab').querySelector('[data-action="run-workflow"]')!,
    );

    await waitFor(() => expect(streamExecutionMock).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('persisted-old')).toBeNull();
    expect(screen.getByText('Running…')).toBeTruthy();
  });

  it('prefills workflow inputs from the last run VFS inputs file', async () => {
    readVfsRunMock.mockResolvedValue({
      path: '/run/__exec__/inputs.json',
      content_type: 'application/json',
      content: '{"query":"from-vfs","limit":7}',
      size_bytes: 30,
      truncated: false,
      wf_version: null,
      stale: false,
    });
    useWorkflowEditStore.getState().setDraft(
      startWith({
        query: { type: 'string', value: '', reference: '' },
        limit: { type: 'number', value: '', reference: '' },
      }),
    );
    renderTab();

    await waitFor(() =>
      expect((screen.getByTestId('exec-input-query-input') as HTMLInputElement).value).toBe(
        'from-vfs',
      ),
    );
    expect((screen.getByTestId('exec-input-limit-input') as HTMLInputElement).value).toBe(
      '7',
    );
    expect(readVfsRunMock).toHaveBeenCalledWith({
      path: '/run/__exec__/inputs.json',
      run_id: 'wf_1',
    });
  });

  it('hydrates persisted inputs once without entering a render loop', async () => {
    readVfsRunMock.mockResolvedValue({
      path: '/run/__exec__/inputs.json',
      content_type: 'application/json',
      content: '{"query":"stable"}',
      size_bytes: 18,
      truncated: false,
      wf_version: null,
      stale: false,
    });
    useWorkflowEditStore.getState().setDraft(
      startWith({ query: { type: 'string', value: '', reference: '' } }),
    );
    let workflowInputWrites = 0;
    const unsubscribe = useExecStreamStore.subscribe((state, previous) => {
      if (state.inputsByWorkflow !== previous.inputsByWorkflow) workflowInputWrites += 1;
    });

    renderTab();
    await waitFor(() =>
      expect((screen.getByTestId('exec-input-query-input') as HTMLInputElement).value).toBe(
        'stable',
      ),
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
    unsubscribe();

    // One initial buffer publication plus one hydrated value publication. A
    // changing fieldNames array used to keep this counter growing indefinitely.
    expect(workflowInputWrites).toBeLessThanOrEqual(3);
  });

  it('does not overwrite user-edited inputs when the VFS prefill resolves later', async () => {
    let resolveRead: (value: unknown) => void = () => {};
    readVfsRunMock.mockReturnValue(new Promise((resolve) => {
      resolveRead = resolve;
    }));
    useWorkflowEditStore.getState().setDraft(
      startWith({ query: { type: 'string', value: '', reference: '' } }),
    );
    renderTab();

    const input = screen.getByTestId('exec-input-query-input') as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'typed' } });
    fireEvent.blur(input);
    resolveRead({
      path: '/run/__exec__/inputs.json',
      content_type: 'application/json',
      content: '{"query":"from-vfs"}',
      size_bytes: 20,
      truncated: false,
      wf_version: null,
      stale: false,
    });

    await waitFor(() => expect(readVfsRunMock).toHaveBeenCalled());
    expect(input.value).toBe('typed');
  });

  it('lets backend validation handle a bad number literal', async () => {
    useWorkflowEditStore.getState().setDraft(
      startWith({ limit: { type: 'number', value: '', reference: '' } }),
    );
    renderTab();
    const input = screen.getByTestId('exec-input-limit-input');
    fireEvent.change(input, { target: { value: 'not-a-number' } });
    fireEvent.blur(input);
    fireEvent.click(
      screen.getByTestId('workflow-run-tab').querySelector('[data-action="run-workflow"]')!,
    );
    await waitFor(() => expect(streamExecutionMock).toHaveBeenCalledTimes(1));
    const arg = streamExecutionMock.mock.calls[0][0] as {
      input: Record<string, unknown>;
    };
    expect(arg.input.limit).toBe('not-a-number');
    expect(screen.queryByTestId('exec-field-error-limit')).toBeNull();
  });

  it('renders no output region when idle with no prior run', async () => {
    getWorkflowExecutionStatus.mockResolvedValue(null);
    useWorkflowEditStore.getState().setDraft(startWith({}));
    renderTab();
    // The input form + Run are present; the output region is absent — never a
    // stale "No execution yet." placeholder.
    expect(screen.getByTestId('workflow-run-tab')).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByTestId('run-output')).toBeNull(),
    );
    expect(screen.queryByText('No execution yet.')).toBeNull();
  });

  it('renders live per-node cards from the streaming store', () => {
    useWorkflowEditStore.getState().setDraft(startWith({}));
    act(() => {
      const s = useExecStreamStore.getState();
      s.begin('wf_1', new AbortController());
      s.applyUpdate({ node_id: 'node_1', status: 'completed', result: 'hi' });
    });
    renderTab();
    expect(screen.getByTestId('exec-status').textContent).toBe('running');
    expect(screen.getByTestId('exec-node-card').getAttribute('data-node-id')).toBe(
      'node_1',
    );
    expect(screen.getByText('hi')).toBeTruthy();
    expect(getWorkflowExecutionStatus).not.toHaveBeenCalled();
  });

  it('shows per-node + end-to-end duration on a live run', () => {
    useWorkflowEditStore.getState().setDraft(startWith({}));
    act(() => {
      const s = useExecStreamStore.getState();
      s.begin('wf_1', new AbortController());
      // A per-node completed frame carrying the node's duration…
      s.applyUpdate({
        node_id: 'node_1',
        status: 'completed',
        result: 'hi',
        duration: 0.42,
      });
      // …then the terminal frame carrying the end-to-end total.
      s.applyUpdate({ status: 'completed', outputs: { y: 1 }, duration: 1.23 });
    });
    renderTab();
    expect(screen.getByTestId('exec-node-duration').textContent).toContain('0.42s');
    expect(screen.getByTestId('exec-total-duration').textContent).toContain('1.23s');
  });

  it('labels a named node card as node_name(node_id)', () => {
    useWorkflowEditStore.getState().setDraft(
      startWith({}),
    );
    // Augment the draft with a second, user-named node so its card title shows
    // `name(node_id)` rather than the bare id.
    act(() => {
      const d = { ...(useWorkflowEditStore.getState().draft as Record<string, unknown>) };
      d.node_2 = {
        node_id: 'node_2',
        node_name: 'my_prompt',
        node_type: 'PromptNode',
        input_fields: {},
        output_fields: {},
        node_config: {},
        children: [],
      };
      useWorkflowEditStore.getState().setDraft(d as never);
    });
    act(() => {
      const s = useExecStreamStore.getState();
      s.begin('wf_1', new AbortController());
      s.applyUpdate({ node_id: 'node_2', status: 'completed', result: 'hi' });
    });
    renderTab();
    const card = screen.getByTestId('exec-node-card');
    expect(card.getAttribute('data-node-id')).toBe('node_2');
    expect(card).toHaveTextContent('my_prompt(node_2)');
  });

  it('hydrates per-node cards from the persisted record after a reload', async () => {
    useWorkflowEditStore.getState().setDraft(startWith({}));
    getWorkflowExecutionStatus.mockResolvedValue({
      exec_id: 'exec_old',
      wf_id: 'wf_1',
      status: 'completed',
      started_at: 1000,
      finished_at: 1002.5,
      result: {
        node_1: {
          status: 'completed',
          execution_result: 'persisted-out',
          duration: 0.5,
        },
        node_2: { status: 'error', error: 'boom' },
      },
    });
    renderTab();
    await waitFor(() => expect(screen.getByText('persisted-out')).toBeTruthy());
    expect(screen.getByText('boom')).toBeTruthy();
    expect(screen.getByTestId('exec-status').textContent).toBe('completed');
    // Persisted per-node duration shows on the node card…
    expect(screen.getByTestId('exec-node-duration').textContent).toContain('0.50s');
    // …and the end-to-end total derives from started/finished timestamps.
    expect(screen.getByTestId('exec-total-duration').textContent).toContain('2.50s');
  });
});
