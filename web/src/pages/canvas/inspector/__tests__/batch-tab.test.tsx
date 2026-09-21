/**
 * BatchTab tests formerly covered by `RunBatchModal.test.tsx` (parseCsv and the
 * source selector, the auto-map → inverted submit, the task-center hand-off
 * toast) onto the inline tab, and adds the NEW workflow-scoped task list +
 * inline progress drill-down.
 *
 * Mocks the un-codegenned tasks client + the wf-task-list query + sonner + the
 * VFS read seam + the task SSE hook so the tab stays a pure unit.
 */
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import i18n from 'i18next';

const {
  submitBatchMock,
  getTaskMock,
  toastSpy,
  useWorkflowTasksMock,
  useTaskStreamMock,
  readVfsMock,
} = vi.hoisted(() => ({
  submitBatchMock: vi.fn(),
  getTaskMock: vi.fn(),
  toastSpy: { success: vi.fn(), error: vi.fn() },
  useWorkflowTasksMock: vi.fn(),
  useTaskStreamMock: vi.fn(),
  readVfsMock: vi.fn(),
}));

vi.mock('@/lib/api/tasks', () => ({
  submitBatch: (...a: unknown[]) => submitBatchMock(...a),
  getTask: (...a: unknown[]) => getTaskMock(...a),
}));
vi.mock('@/lib/api/queries/tasks', () => ({
  useWorkflowTasks: (...a: unknown[]) => useWorkflowTasksMock(...a),
}));
vi.mock('@/lib/api/sse/run-task-stream', () => ({
  useTaskStream: (...a: unknown[]) => useTaskStreamMock(...a),
}));
vi.mock('@/lib/api/vfs', () => ({
  readVfs: (...a: unknown[]) => readVfsMock(...a),
}));
// exceljs's zip/stream path hangs under jsdom, so mock the parser here (the
// wiring is what this unit tests) — the real parseExcel is round-tripped in a
// node-env test (lib/batch/__tests__/excel.test.ts).
vi.mock('@/lib/batch/excel', () => ({
  parseExcel: vi.fn(async (_buf: ArrayBuffer, sheet?: string) => {
    const bySheet: Record<string, { x: string }[]> = {
      Alpha: [{ x: '1' }, { x: '2' }],
      Beta: [{ x: '9' }],
    };
    return { columns: ['x'], rows: bySheet[sheet ?? 'Alpha'], sheetNames: ['Alpha', 'Beta'] };
  }),
}));
vi.mock('sonner', () => ({ toast: toastSpy }));

import { BatchTab } from '@/pages/canvas/inspector/BatchTab';
import { parseCsv } from '@/pages/canvas/inspector/batch-csv';
import { useWorkflowEditStore } from '@/stores/workflow-edit';

Element.prototype.scrollIntoView ??= vi.fn();

// The fixed output columns (default state), present in every submit body.
const FIXED_COLUMNS = [
  { kind: 'index', name: 'index' },
  { kind: 'status', name: 'status' },
  { kind: 'error', name: 'error' },
  { kind: 'execution_time', name: 'execution_time' },
];

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: {} } },
  interpolation: { escapeValue: false },
});

function startWith(fields: Record<string, unknown>) {
  return {
    node_1: {
      node_id: 'node_1',
      node_name: '__start__',
      node_type: 'StartNode',
      input_fields: fields,
      children: [],
    },
    __meta__: {},
  };
}

function renderTab(node: React.ReactNode = <BatchTab wfId="wf_1" />) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={testI18n}>
        <MemoryRouter initialEntries={['/workflow/wf_1']}>
          <Routes>
            <Route path="/workflow/:wfId" element={<>{node}</>} />
            <Route path="/tasks" element={<div>task center</div>} />
            <Route path="/tasks/:taskId" element={<div>task page</div>} />
          </Routes>
        </MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  );
}

describe('parseCsv', () => {
  it('uses the first row as header and maps each row by column', () => {
    const out = parseCsv('a,b\n1,2\n3,4\n');
    expect(out.columns).toEqual(['a', 'b']);
    expect(out.rows).toEqual([
      { a: '1', b: '2' },
      { a: '3', b: '4' },
    ]);
  });
  it('returns empty for blank input', () => {
    expect(parseCsv('')).toEqual({ columns: [], rows: [] });
  });
  it('tolerates short rows + CRLF', () => {
    expect(parseCsv('a,b\r\n1\r\n').rows).toEqual([{ a: '1', b: '' }]);
  });
  it('preserves quoted commas, newlines, and escaped quotes', () => {
    expect(parseCsv('name,note\r\n"Ada, A.","line 1\nline 2"\r\nBob,"said ""hi"""').rows).toEqual([
      { name: 'Ada, A.', note: 'line 1\nline 2' },
      { name: 'Bob', note: 'said "hi"' },
    ]);
  });
});

describe('<BatchTab> submit + source selector', () => {
  beforeEach(() => {
    // Batch config persists per-wfId in localStorage — clear so it doesn't bleed
    // between tests (they all use wfId="wf_1").
    localStorage.clear();
    submitBatchMock.mockReset();
    toastSpy.success.mockReset();
    toastSpy.error.mockReset();
    readVfsMock.mockReset();
    useTaskStreamMock.mockReturnValue({ events: [], done: false });
    useWorkflowTasksMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    useWorkflowEditStore.getState().setDraft(startWith({ x: { type: 'string' } }));
  });

  it('renders only implemented source tabs', () => {
    renderTab();
    expect(screen.getByTestId('batch-source-selector')).toBeInTheDocument();
    expect(screen.getByTestId('batch-source-upload')).toBeInTheDocument();
    expect(screen.getByTestId('batch-source-data')).toBeInTheDocument();
    expect(screen.queryByTestId('batch-source-url')).toBeNull();
    expect(screen.queryByTestId('batch-url-input')).toBeNull();
  });

  it('disables submit when no rows are loaded', () => {
    renderTab();
    expect(screen.getByTestId('batch-submit')).toBeDisabled();
  });

  it('parses CSV, auto-maps same-name columns, submits the inverted mapping', async () => {
    submitBatchMock.mockResolvedValueOnce({ task_id: 'tk_1' });
    renderTab();
    const input = screen.getByTestId('batch-csv-input') as HTMLInputElement;
    const file = new File(['x\n1\n2\n'], 'rows.csv', { type: 'text/csv' });
    fireEvent.change(input, { target: { files: [file] } });

    const submit = await screen.findByTestId('batch-submit');
    await waitFor(() => expect(submit).not.toBeDisabled());
    fireEvent.click(submit);

    await waitFor(() => expect(submitBatchMock).toHaveBeenCalledTimes(1));
    expect(submitBatchMock).toHaveBeenCalledWith('wf_1', {
      data_source: { rows: [{ x: '1' }, { x: '2' }] },
      column_mapping: { x: 'x' },
      // No output location typed → null (results stay in the downloadable copy).
      output: null,
      concurrency: 1,
      output_columns: FIXED_COLUMNS,
    });
    await waitFor(() => expect(toastSpy.success).toHaveBeenCalledTimes(1));
    const [msg, opts] = toastSpy.success.mock.calls[0] as [
      string,
      { action: { label: string; onClick: () => void } },
    ];
    expect(msg).toMatch(/started/i);
    expect(typeof opts.action.onClick).toBe('function');
  });

  it('sends the output destination spec when an output path is typed', async () => {
    submitBatchMock.mockResolvedValueOnce({ task_id: 'tk_2' });
    renderTab();
    const input = screen.getByTestId('batch-csv-input') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(['x\n1\n'], 'rows.csv', { type: 'text/csv' })] },
    });
    const outPath = await screen.findByTestId('batch-output-path');
    fireEvent.change(outPath, { target: { value: '/data/results.csv' } });

    const submit = await screen.findByTestId('batch-submit');
    await waitFor(() => expect(submit).not.toBeDisabled());
    fireEvent.click(submit);

    await waitFor(() => expect(submitBatchMock).toHaveBeenCalledTimes(1));
    expect(submitBatchMock).toHaveBeenCalledWith('wf_1', {
      data_source: { rows: [{ x: '1' }] },
      column_mapping: { x: 'x' },
      output: { type: 'vfs_data', path: '/data/results.csv' },
      concurrency: 1,
      output_columns: FIXED_COLUMNS,
    });
  });

  it('reveals a sheet-name field for an Excel output path and sends it', async () => {
    submitBatchMock.mockResolvedValueOnce({ task_id: 'tk_3' });
    renderTab();
    const input = screen.getByTestId('batch-csv-input') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(['x\n1\n'], 'rows.csv', { type: 'text/csv' })] },
    });
    const outPath = await screen.findByTestId('batch-output-path');
    // No sheet field for a CSV path.
    expect(screen.queryByTestId('batch-output-sheet')).toBeNull();
    // An .xlsx path reveals it.
    fireEvent.change(outPath, { target: { value: '/data/out.xlsx' } });
    const sheet = await screen.findByTestId('batch-output-sheet');
    fireEvent.change(sheet, { target: { value: 'Results' } });

    const submit = await screen.findByTestId('batch-submit');
    await waitFor(() => expect(submit).not.toBeDisabled());
    fireEvent.click(submit);

    await waitFor(() => expect(submitBatchMock).toHaveBeenCalledTimes(1));
    expect(submitBatchMock).toHaveBeenCalledWith('wf_1', {
      data_source: { rows: [{ x: '1' }] },
      column_mapping: { x: 'x' },
      output: { type: 'vfs_data', path: '/data/out.xlsx', sheet_name: 'Results' },
      concurrency: 1,
      output_columns: FIXED_COLUMNS,
    });
  });

  it('parses an uploaded Excel file and reads the selected sheet', async () => {
    submitBatchMock.mockResolvedValueOnce({ task_id: 'tk_x' });
    renderTab();
    const input = screen.getByTestId('batch-csv-input') as HTMLInputElement;
    // Content is irrelevant — parseExcel is mocked; the .xlsx extension routes it.
    fireEvent.change(input, { target: { files: [new File(['x'], 'data.xlsx')] } });

    // The read-sheet picker appears listing both sheets.
    const picker = await screen.findByTestId('batch-input-sheet');
    fireEvent.click(picker);
    expect(await screen.findByRole('option', { name: 'Alpha' })).toBeInTheDocument();
    expect(await screen.findByRole('option', { name: 'Beta' })).toBeInTheDocument();

    // Switch to Beta → re-parses that sheet's rows.
    fireEvent.click(screen.getByRole('option', { name: 'Beta' }));
    const submit = await screen.findByTestId('batch-submit');
    await waitFor(() => expect(submit).not.toBeDisabled());
    fireEvent.click(submit);

    await waitFor(() => expect(submitBatchMock).toHaveBeenCalledTimes(1));
    expect(submitBatchMock).toHaveBeenCalledWith('wf_1', {
      data_source: { rows: [{ x: '9' }] },
      column_mapping: { x: 'x' },
      output: null,
      concurrency: 1,
      output_columns: FIXED_COLUMNS,
    });
  });

  it('sends the chosen parallel-rows (concurrency) value', async () => {
    submitBatchMock.mockResolvedValueOnce({ task_id: 'tk_c' });
    renderTab();
    const input = screen.getByTestId('batch-csv-input') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(['x\n1\n'], 'rows.csv', { type: 'text/csv' })] },
    });
    const conc = await screen.findByTestId('batch-concurrency');
    fireEvent.change(conc, { target: { value: '4' } });

    const submit = await screen.findByTestId('batch-submit');
    await waitFor(() => expect(submit).not.toBeDisabled());
    fireEvent.click(submit);

    await waitFor(() => expect(submitBatchMock).toHaveBeenCalledTimes(1));
    expect(submitBatchMock.mock.calls[0][1]).toMatchObject({ concurrency: 4 });
  });

  it('persists the inference config (output + concurrency) across remounts', async () => {
    const { unmount } = renderTab();
    fireEvent.change(screen.getByTestId('batch-output-path'), {
      target: { value: '/data/keep.csv' },
    });
    fireEvent.change(screen.getByTestId('batch-concurrency'), { target: { value: '5' } });
    // Let the save effect flush.
    await waitFor(() =>
      expect((screen.getByTestId('batch-output-path') as HTMLInputElement).value).toBe(
        '/data/keep.csv',
      ),
    );
    unmount();

    // Re-enter the Batch page → config restored from the per-workflow store.
    renderTab();
    expect((screen.getByTestId('batch-output-path') as HTMLInputElement).value).toBe(
      '/data/keep.csv',
    );
    expect((screen.getByTestId('batch-concurrency') as HTMLInputElement).value).toBe('5');
  });

  it('surfaces an error toast on submit failure', async () => {
    submitBatchMock.mockRejectedValueOnce(new Error('boom'));
    renderTab();
    const input = screen.getByTestId('batch-csv-input') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(['x\n1\n'], 'r.csv', { type: 'text/csv' })] },
    });
    const submit = await screen.findByTestId('batch-submit');
    await waitFor(() => expect(submit).not.toBeDisabled());
    fireEvent.click(submit);
    await waitFor(() => expect(toastSpy.error).toHaveBeenCalledTimes(1));
    expect(toastSpy.success).not.toHaveBeenCalled();
  });

  it('renders the fixed output columns after parsing a file', async () => {
    renderTab();
    fireEvent.change(screen.getByTestId('batch-csv-input') as HTMLInputElement, {
      target: { files: [new File(['x\n1\n'], 'rows.csv', { type: 'text/csv' })] },
    });
    expect(await screen.findByTestId('batch-col-fixed-index')).toBeInTheDocument();
    expect(screen.getByTestId('batch-col-fixed-status')).toBeInTheDocument();
    expect(screen.getByTestId('batch-col-fixed-error')).toBeInTheDocument();
    expect(screen.getByTestId('batch-col-fixed-execution_time')).toBeInTheDocument();
  });

  it('adds a field column, picks a source, and sends it with the fixed columns', async () => {
    submitBatchMock.mockResolvedValueOnce({ task_id: 'tk_col' });
    // A draft whose StartNode has an output field to offer as a source.
    useWorkflowEditStore.getState().setDraft({
      node_1: {
        node_id: 'node_1',
        node_name: 'start',
        node_type: 'StartNode',
        input_fields: { x: { type: 'string' } },
        output_fields: { score: { type: 'string' } },
        children: [],
      },
      __meta__: {},
    });
    renderTab();
    fireEvent.change(screen.getByTestId('batch-csv-input') as HTMLInputElement, {
      target: { files: [new File(['x\n1\n'], 'rows.csv', { type: 'text/csv' })] },
    });

    fireEvent.click(await screen.findByTestId('batch-col-add'));
    const source = await screen.findByTestId('batch-col-source');
    fireEvent.click(source);
    // The source lists the node output `start.score`. Pick it through the same
    // menu path a user takes so the test stays agnostic to the encoded value.
    fireEvent.click(await screen.findByRole('option', { name: 'start.score' }));
    // Name the column header.
    fireEvent.change(screen.getByTestId('batch-col-name'), {
      target: { value: 'My Score' },
    });

    const submit = await screen.findByTestId('batch-submit');
    await waitFor(() => expect(submit).not.toBeDisabled());
    fireEvent.click(submit);

    await waitFor(() => expect(submitBatchMock).toHaveBeenCalledTimes(1));
    expect(submitBatchMock.mock.calls[0][1]).toMatchObject({
      output_columns: [
        ...FIXED_COLUMNS,
        { kind: 'field', name: 'My Score', node: 'start', field: 'score' },
      ],
    });
  });

  it('skips an incomplete (no-source) field column', async () => {
    submitBatchMock.mockResolvedValueOnce({ task_id: 'tk_skip' });
    renderTab();
    fireEvent.change(screen.getByTestId('batch-csv-input') as HTMLInputElement, {
      target: { files: [new File(['x\n1\n'], 'rows.csv', { type: 'text/csv' })] },
    });
    // Add a card but never choose a source.
    fireEvent.click(await screen.findByTestId('batch-col-add'));

    const submit = await screen.findByTestId('batch-submit');
    await waitFor(() => expect(submit).not.toBeDisabled());
    fireEvent.click(submit);

    await waitFor(() => expect(submitBatchMock).toHaveBeenCalledTimes(1));
    // Only the fixed columns survive — the half-configured card is skipped.
    expect(submitBatchMock.mock.calls[0][1]).toMatchObject({
      output_columns: FIXED_COLUMNS,
    });
  });

  it('loads tabular data from a typed /mount path', async () => {
    submitBatchMock.mockResolvedValueOnce({ task_id: 'tk_data' });
    readVfsMock.mockResolvedValueOnce({
      path: '/mount/rows.jsonl',
      content_type: 'table/jsonl',
      content: '{"x":"1"}\n{"x":"2"}\n',
      size_bytes: 20,
      truncated: false,
    });
    renderTab();
    fireEvent.click(screen.getByTestId('batch-source-data'));
    fireEvent.change(screen.getByTestId('batch-data-picker'), {
      target: { value: '/mount/rows.jsonl' },
    });
    fireEvent.click(screen.getByTestId('batch-data-load'));

    const submit = await screen.findByTestId('batch-submit');
    await waitFor(() => expect(submit).not.toBeDisabled());
    fireEvent.click(submit);

    await waitFor(() => expect(readVfsMock).toHaveBeenCalledTimes(1));
    expect(readVfsMock).toHaveBeenCalledWith({
      path: '/mount/rows.jsonl',
      wf_id: 'wf_1',
    });
    await waitFor(() => expect(submitBatchMock).toHaveBeenCalledTimes(1));
    expect(submitBatchMock.mock.calls[0][1]).toMatchObject({
      data_source: { rows: [{ x: '1' }, { x: '2' }] },
      column_mapping: { x: 'x' },
      output_columns: FIXED_COLUMNS,
    });
  });
});

describe('<BatchTab> this-workflow task list + inline progress', () => {
  beforeEach(() => {
    useTaskStreamMock.mockReturnValue({ events: [], done: false });
    getTaskMock.mockReset();
    useWorkflowEditStore.getState().setDraft(startWith({ x: { type: 'string' } }));
  });

  it('shows a no-batch-yet empty state when there are no tasks', () => {
    useWorkflowTasksMock.mockReturnValue({ data: { items: [] }, isLoading: false });
    renderTab();
    expect(screen.getByTestId('batch-no-tasks')).toBeInTheDocument();
  });

  it('lists this workflow’s batch runs and links to the Task Center', () => {
    useWorkflowTasksMock.mockReturnValue({
      data: {
        items: [
          {
            id: 'task_abcdef12',
            status: 'running',
            progress: 0.5,
            task_type: 'batch_exec',
            workflow_id: 'wf_1',
            submitted_at: '2026-06-10T00:00:00Z',
          },
        ],
      },
      isLoading: false,
    });
    renderTab();
    const rows = screen.getAllByTestId('batch-task-row');
    expect(rows).toHaveLength(1);
    expect(screen.getByTestId('batch-task-status').textContent).toMatch(/running/i);
    expect(screen.getByTestId('batch-view-task-center')).toHaveAttribute('href', '/tasks');
  });

  it('clicking a task opens the inline progress view (reusing the SSE hook)', async () => {
    useWorkflowTasksMock.mockReturnValue({
      data: {
        items: [
          {
            id: 'task_abcdef12',
            status: 'running',
            progress: 0.4,
            task_type: 'batch_exec',
            workflow_id: 'wf_1',
            submitted_at: '2026-06-10T00:00:00Z',
          },
        ],
      },
      isLoading: false,
    });
    getTaskMock.mockResolvedValue({
      id: 'task_abcdef12',
      status: 'running',
      progress: 0.4,
      task_type: 'batch_exec',
      workflow_id: 'wf_1',
      submitted_at: '2026-06-10T00:00:00Z',
      started_at: null,
      finished_at: null,
      payload: null,
      result: null,
      results_uri: null,
      error: null,
      background_job_id: null,
    });
    useTaskStreamMock.mockReturnValue({
      events: [{ id: 1, event_type: 'progress', payload: { done: 2, total: 5 } }],
      done: false,
    });
    renderTab();
    fireEvent.click(screen.getByTestId('batch-task-row'));
    await waitFor(() =>
      expect(screen.getByTestId('batch-task-progress')).toBeInTheDocument(),
    );
    expect(useTaskStreamMock).toHaveBeenCalledWith('task_abcdef12');
    // Back returns to the list.
    fireEvent.click(screen.getByTestId('batch-task-back'));
    await waitFor(() =>
      expect(screen.getByTestId('batch-task-list')).toBeInTheDocument(),
    );
  });
});
