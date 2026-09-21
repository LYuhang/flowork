/**
 * `TasksListPage` smoke test.
 *
 * Goal: prove the page mounts inside the Provider stack and renders a
 * row for each task returned by `listTasks`. We mock the module directly
 * (rather than via MSW) because `tasks.ts` reaches around `apiClient`
 * for the un-codegenned endpoints, and a module mock is the simplest
 * way to assert the page consumes its contract correctly without also
 * exercising the auth store init.
 *
 * The render assertion is intentionally narrow — we check that the
 * workflow id from the mocked row appears in the DOM. The status badge
 * + type label + progress bar are all derived from the same task object,
 * so finding the row is enough to prove the table-render path is wired.
 * Detail-page interactions and cancel flow get coverage in T15.
 */
import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import i18n from 'i18next';

import type { Task, TaskStatus } from '@/lib/api/tasks';

vi.mock('@/lib/api/tasks', () => ({
  listTasks: vi.fn(),
  getTaskSummary: vi.fn(),
  cancelTask: vi.fn(),
  resumeTask: vi.fn(),
}));

vi.mock('@/lib/api/queries/workflows', () => ({
  useWorkspaceList: vi.fn(() => ({ data: { items: [] }, isLoading: false })),
}));
vi.mock('@/lib/api/queries/workflow', () => ({
  useWorkflow: vi.fn(() => ({ data: null, isLoading: false, isError: false })),
}));
vi.mock('@/pages/canvas/inspector/BatchTab', () => ({
  BatchTab: () => <div data-testid="mock-batch-tab">Batch form</div>,
}));

import { getTaskSummary, listTasks } from '@/lib/api/tasks';
import { TasksListPage } from '@/pages/tasks/TasksListPage';

function makeTask(over: Partial<Task> & { id: string; status: TaskStatus }): Task {
  return {
    progress: 0.5,
    task_type: 'batch_exec',
    workflow_id: 'wf_42',
    payload: {},
    result: null,
    results_uri: null,
    error: null,
    background_job_id: 'background-1',
    submitted_at: '2026-05-24T00:00:00Z',
    started_at: null,
    finished_at: null,
    access: { capabilities: ['view', 'export', 'update', 'delete', 'manage_access', 'execute', 'cancel', 'resume', 'inspect_runs'], effective_role: 'manager', source: 'computed' },
    provenance: {
      ownership_scope: 'personal',
      origin_type: 'created',
      owner: { type: 'user', display_name: 'Task owner' },
      created_by: { type: 'user', display_name: 'Task owner' },
    },
    ...over,
  };
}

const DEFAULT_ITEMS: Task[] = [
  makeTask({
    id: '00000000-0000-0000-0000-000000000001',
    status: 'running',
    workflow_id: 'wf_42',
  }),
];

function mockList(items: Task[]) {
  vi.mocked(listTasks).mockResolvedValue({ items, total: items.length, limit: 50, offset: 0 });
  const running = items.filter((task) => task.status === 'running').length;
  const queued = items.filter((task) => task.status === 'queued').length;
  const cancelling = items.filter((task) => task.status === 'cancelling').length;
  vi.mocked(getTaskSummary).mockResolvedValue({
    active: running + queued + cancelling,
    queued,
    running,
    cancelling,
    failed: items.filter((task) => task.status === 'failed').length,
    finished: items.filter((task) => task.status === 'finished').length,
    cancelled: items.filter((task) => task.status === 'cancelled').length,
  });
}

// Local i18n instance — empty resources mean every `t(key, default)` call
// falls back to the inline default string (matches the WorkspacePage test
// pattern). Keeps the test independent of the actual locale JSON files.
const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: {} } },
  interpolation: { escapeValue: false },
});

function renderWithProviders(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={testI18n}>
        <MemoryRouter>{ui}</MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  );
}

describe('<TasksListPage>', () => {
  beforeEach(() => {
    vi.mocked(listTasks).mockReset();
    vi.mocked(getTaskSummary).mockReset();
    mockList(DEFAULT_ITEMS);
  });

  it('renders the page header without throwing', () => {
    renderWithProviders(<TasksListPage />);
    expect(screen.getByText('Task')).toBeInTheDocument();
  });

  it('renders a row for each task returned by the API', async () => {
    renderWithProviders(<TasksListPage />);
    await waitFor(() =>
      expect(screen.getByText('wf_42')).toBeInTheDocument(),
    );
  });

  it('describes the supported search fields and labels task sharing precisely', async () => {
    const user = userEvent.setup();
    renderWithProviders(<TasksListPage />);

    expect(
      await screen.findByPlaceholderText('Search task or workflow ID'),
    ).toBeInTheDocument();
    await screen.findByText('wf_42');
    await user.click(screen.getByRole('button', { name: 'Open task actions' }));
    expect(await screen.findByText('Share task')).toBeInTheDocument();
  });

  it('surfaces a running-task indicator badge when a task is active', async () => {
    mockList([
      makeTask({ id: 'r1', status: 'running', workflow_id: 'wf_run' }),
      makeTask({ id: 'f1', status: 'finished', workflow_id: 'wf_done' }),
    ]);
    renderWithProviders(<TasksListPage />);
    const badge = await screen.findByTestId('tasks-running-badge');
    expect(badge).toHaveTextContent('1 running');
  });

  it('hides the running badge when no task is active', async () => {
    mockList([
      makeTask({ id: 'f1', status: 'finished', workflow_id: 'wf_done' }),
    ]);
    renderWithProviders(<TasksListPage />);
    await waitFor(() => expect(screen.getByText('wf_done')).toBeInTheDocument());
    expect(screen.queryByTestId('tasks-running-badge')).not.toBeInTheDocument();
  });

  it('sorts active (running) tasks above finished ones', async () => {
    mockList([
      // finished submitted later, running submitted earlier — running must
      // still float to the top because active work is what the user wants.
      makeTask({
        id: 'f1',
        status: 'finished',
        workflow_id: 'wf_done',
        submitted_at: '2026-05-24T02:00:00Z',
      }),
      makeTask({
        id: 'r1',
        status: 'running',
        workflow_id: 'wf_run',
        submitted_at: '2026-05-24T01:00:00Z',
      }),
    ]);
    renderWithProviders(<TasksListPage />);
    await waitFor(() => expect(screen.getByText('wf_run')).toBeInTheDocument());
    const rows = screen.getAllByText(/wf_(run|done)/);
    expect(rows[0]).toHaveTextContent('wf_run');
    expect(rows[1]).toHaveTextContent('wf_done');
  });

  it('opens the in-page batch task creation flow from New Task', async () => {
    const user = userEvent.setup();
    renderWithProviders(<TasksListPage />);
    await user.click(screen.getByRole('button', { name: /new task/i }));
    await user.click((await screen.findAllByText('Batch execution')).at(-1)!);
    expect(await screen.findByText('Batch execution setup')).toBeInTheDocument();
    expect(
      screen.getByText('Create a workflow before starting a batch task.'),
    ).toBeInTheDocument();
    expect(document.querySelector('[data-role="task-create-scroll-region"]')).toHaveClass(
      'page-scroll-region',
      'flex-1',
    );
  });

  it('shows one Cancel action in the scheduled-run setup footer', async () => {
    const user = userEvent.setup();
    renderWithProviders(<TasksListPage />);
    await user.click(screen.getByRole('button', { name: /new task/i }));
    await user.click((await screen.findAllByText('Scheduled run')).at(-1)!);

    expect(await screen.findByText('Scheduled run setup')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Cancel' })).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Finish' })).toBeInTheDocument();
  });

  it('offers precise calendar and monthly schedule controls', async () => {
    const user = userEvent.setup();
    renderWithProviders(<TasksListPage />);
    await user.click(screen.getByRole('button', { name: /new task/i }));
    await user.click((await screen.findAllByText('Scheduled run')).at(-1)!);

    await user.click(screen.getByRole('combobox', { name: 'Frequency' }));
    await user.click(screen.getByRole('option', { name: 'Monthly' }));
    expect(screen.getByText('Day of month')).toBeInTheDocument();
    expect(screen.getByText('Run time')).toBeInTheDocument();
    expect(screen.getByText(/previous run is still active/i)).toBeInTheDocument();
  });

  it('keeps status choices in one multi-select and renders only selected chips', async () => {
    const user = userEvent.setup();
    renderWithProviders(<TasksListPage />);
    await screen.findByText('wf_42');

    expect(screen.queryByLabelText('Selected status filters')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Filter by status' }));
    await user.click(screen.getByRole('menuitemcheckbox', { name: 'running' }));
    await user.click(screen.getByRole('menuitemcheckbox', { name: 'failed' }));

    const selected = screen.getByLabelText('Selected status filters');
    expect(selected).toHaveTextContent('running');
    expect(selected).toHaveTextContent('failed');
    await waitFor(() => expect(listTasks).toHaveBeenLastCalledWith(expect.objectContaining({
      status: ['running', 'failed'],
      offset: 0,
    })));
  });
});
