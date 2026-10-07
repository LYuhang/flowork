/**
 * Deployments T14 — `DeploymentDetailPage` smoke test.
 *
 * Mocks the deployments API so the page can render against a stable
 * fixture. Asserts:
 *   * The six tab triggers appear.
 *   * The Overview tab — the default selected tab — surfaces the deployment
 *     endpoint and status.
 *   * The "Rotate API key" button appears for trigger_type=api
 *     deployments (it's hidden for webhook, but T14 covers the
 *     api path; the conditional render is exercised here).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import i18n from 'i18next';
import { TooltipProvider } from '@/components/ui/tooltip';

const DEP_ID = '00000000-0000-0000-0000-000000000abc';

vi.mock('@/lib/api/deployments', () => ({
  getDeployment: vi.fn(async () => ({
    id: DEP_ID,
    tenant_id: '00000000-0000-0000-0000-0000000000aa',
    user_id: '00000000-0000-0000-0000-0000000000bb',
    wf_id: 'wf_42',
    name: 'API bot',
    slug: 'bot',
    trigger_type: 'api',
    version_pin: 'specific',
    pinned_major: 1,
    pinned_sub: 0,
    enabled: true,
    rate_limit_qps: 10,
    invoke_count: 5,
    last_invoked_at: null,
    access: {
      capabilities: [
        'view',
        'update',
        'inspect_runs',
        'execute',
        'manage_secret',
        'manage_access',
      ],
      effective_role: 'manager',
      source: 'computed',
    },
    created_at: '2026-05-24T00:00:00Z',
    updated_at: null,
    deleted_at: null,
  })),
  getMetrics: vi.fn(async () => ({
    series: [],
    bucket: 'hour',
    from: '2026-05-23T00:00:00Z',
    to: '2026-05-24T00:00:00Z',
  })),
  getHistory: vi.fn(async () => ({
    items: [],
    next_cursor: null,
    limit: 50,
  })),
  patchDeployment: vi.fn(),
  rotateKey: vi.fn(async () => ({ api_key: 'one-time-test-key' })),
  testInvoke: vi.fn(),
}));

vi.mock('@/lib/api/queries/workflow', () => ({
  useWorkflowVersions: () => ({ data: { versions: [{ major: 1, sub: 0 }] }, isLoading: false }),
}));
vi.mock('@/lib/preview/instance-workflow', () => ({
  loadInstanceWorkflow: vi.fn(async () => ({ workflow: {
    node_1: { node_type: 'StartNode', input_fields: { analysis_focus: { type: 'string' } } },
  } })),
}));

import { DeploymentDetailPage } from '@/pages/deployments/DeploymentDetailPage';
import { getDeployment, getHistory, getMetrics, patchDeployment, rotateKey, testInvoke } from '@/lib/api/deployments';

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: { 'logs.range.30d': 'Last 30 days' } } },
  interpolation: { escapeValue: false },
});

function renderAt(depId: string) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={testI18n}>
        <TooltipProvider>
          <MemoryRouter initialEntries={[`/deployments/${depId}`]}>
            <Routes>
              <Route
                path="/deployments/:depId"
                element={<DeploymentDetailPage />}
              />
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </I18nextProvider>
    </QueryClientProvider>,
  );
}

describe('<DeploymentDetailPage>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getHistory).mockResolvedValue({
      items: [],
      next_cursor: null,
      limit: 50,
    });
  });

  it('shows shared viewer configuration without mutation or secret controls', async () => {
    const dep = await getDeployment(DEP_ID);
    vi.mocked(getDeployment).mockResolvedValueOnce({ ...dep, access: {
      capabilities: ['view', 'inspect_runs'], effective_role: 'viewer', source: 'computed',
    } });
    const user = userEvent.setup();
    renderAt(DEP_ID);
    await screen.findByRole('heading', { name: 'API bot' });
    await user.click(screen.getByRole('tab', { name: /^Settings$/i }));
    for (const label of ['Rate limit (QPS)', 'Call timeout (seconds)', 'CPU cores', 'Memory (MiB)', 'Worker processes', 'Concurrent executions per worker']) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.queryByRole('button', { name: 'Save changes' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /rotate api key/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('spinbutton')).not.toBeInTheDocument();
  });

  it('links an accepted approval invocation to its execution without resubmitting', async () => {
    const user = userEvent.setup();
    vi.mocked(testInvoke).mockResolvedValue({
      invocation_id: 'approval-run', execution_id: 'approval-run',
      execution_url: '/workflow-executions/approval-run',
      result_url: '/api/v1/workflow-executions/approval-run', status: 'waiting_approval',
    });
    renderAt(DEP_ID);
    await screen.findByRole('heading', { level: 1, name: 'API bot' });
    await user.click(screen.getByRole('tab', { name: /^Usage$/i }));
    await user.click(screen.getByRole('button', { name: /^Run$/i }));
    const link = await screen.findByRole('link', { name: 'execution.detail' });
    expect(link).toHaveAttribute('href', '/workflow-executions/approval-run');
    expect(testInvoke).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/"status": "waiting_approval"/)).toBeInTheDocument();
  });

  it('renders the simplified detail sections and the Overview content', async () => {
    const user = userEvent.setup();
    renderAt(DEP_ID);

    // Header — deployment name surfaces once the query resolves.
    await waitFor(() => {
      expect(screen.getByRole('heading', { level: 1, name: 'API bot' })).toBeInTheDocument();
    });
    // The endpoint belongs to Usage, not the overview/header.
    expect(screen.queryByText('/api/v1/deployments/bot/invoke')).not.toBeInTheDocument();

    // Four coherent tab triggers are rendered (Radix Tabs renders each
    // <TabsTrigger> as a real button regardless of which is active).
    expect(
      screen.getByRole('tab', { name: /^Overview$/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('tab', { name: /^Usage$/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('tab', { name: /^Activity$/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('tab', { name: /^Settings$/i }),
    ).toBeInTheDocument();

    // Overview focuses on runtime; editable identity lives in Settings.
    expect(screen.getAllByText('Active').length).toBeGreaterThan(0);
    expect(screen.queryByText('Basic information')).not.toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: /^Settings$/i }));
    expect(screen.getByText('Basic information')).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Name' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Rename' }));
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveValue('API bot');
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('textbox', { name: 'Name' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('tab', { name: /^Usage$/i }));
    expect(screen.getAllByText(/deployments\/bot\/invoke/).length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: /copy endpoint/i })).toBeInTheDocument();

    // Settings keeps high-risk API key rotation explicit.
    await user.click(screen.getByRole('tab', { name: /^Settings$/i }));
    expect(screen.getByText('Traffic and runtime controls')).toBeInTheDocument();
    expect(screen.getByText('Basic information')).toBeInTheDocument();
    expect(
      await screen.findByRole('button', { name: /rotate api key/i }),
    ).toBeInTheDocument();
  });

  it('uses the workflow StartNode fields in code examples and test inputs', async () => {
    const user = userEvent.setup();
    renderAt(DEP_ID);

    await screen.findByRole('heading', { level: 1, name: 'API bot' });
    await user.click(screen.getByRole('tab', { name: /^Usage$/i }));
    expect(screen.getByTestId('deployment-code-curl')).toHaveTextContent(
      '"analysis_focus":"<analysis_focus>"',
    );

    expect(screen.getByRole('textbox', { name: 'Inputs (JSON)' })).toHaveValue(
      '{\n  "analysis_focus": "<analysis_focus>"\n}',
    );
  });

  it('loads only the recent top 50 activity records with an explicit order', async () => {
    const user = userEvent.setup();
    renderAt(DEP_ID);
    await screen.findByRole('heading', { level: 1, name: 'API bot' });
    await user.click(screen.getByRole('tab', { name: /^Activity$/i }));
    await user.click(screen.getByRole('button', { name: /^Request records/ }));

    await waitFor(() => {
      expect(getHistory).toHaveBeenCalledWith(DEP_ID, expect.objectContaining({
        limit: 50,
        order: 'desc',
      }));
    });
    const [, params] = vi.mocked(getHistory).mock.calls.at(-1)!;
    expect(params).not.toHaveProperty('from');
    expect(screen.getByRole('combobox', { name: 'Time range' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Sort' })).toBeInTheDocument();
    const scrollRegion = screen.getByRole('region', { name: 'Deployment run history' });
    expect(scrollRegion).toHaveAttribute('data-role', 'deployment-run-log-scroll-region');
    expect(scrollRegion).toHaveClass('overflow-auto', 'overscroll-contain');
  });

  it('loads an older cursor page inside the bounded run-history region', async () => {
    vi.mocked(getHistory)
      .mockResolvedValueOnce({
        items: [{
          id: 'run-new',
          status: 'succeeded',
          source: 'test',
          trigger_type: 'api',
          submitted_at: '2026-05-24T10:00:00Z',
          started_at: '2026-05-24T10:00:01Z',
          finished_at: '2026-05-24T10:00:02Z',
          latency_ms: 1000,
          error: null,
          task_type: 'deployment_invoke',
        }],
        next_cursor: 'older-cursor',
        limit: 50,
      })
      .mockResolvedValueOnce({
        items: [{
          id: 'run-older',
          status: 'failed',
          source: 'api',
          trigger_type: 'api',
          submitted_at: '2026-05-23T10:00:00Z',
          started_at: '2026-05-23T10:00:01Z',
          finished_at: '2026-05-23T10:00:02Z',
          latency_ms: 1000,
          error: 'request failed',
          task_type: 'deployment_invoke',
        }],
        next_cursor: null,
        limit: 50,
      });

    const user = userEvent.setup();
    renderAt(DEP_ID);
    await screen.findByRole('heading', { level: 1, name: 'API bot' });
    await user.click(screen.getByRole('tab', { name: /^Activity$/i }));
    await user.click(screen.getByRole('button', { name: /^Request records/ }));

    expect((await screen.findAllByText('run-new')).length).toBeGreaterThan(0);
    await user.click(screen.getByRole('button', { name: 'Load older records' }));
    expect((await screen.findAllByText('run-older')).length).toBeGreaterThan(0);
    expect(getHistory).toHaveBeenLastCalledWith(DEP_ID, expect.objectContaining({
      cursor: 'older-cursor',
      limit: 50,
      order: 'desc',
    }));
    expect(screen.getByRole('region', { name: 'Deployment run history' })
      .querySelector('[title="run-older"]')).toBeInTheDocument();
  });

  it('deduplicates overlapping cursor pages and forwards status, range, and ascending order', async () => {
    vi.mocked(getHistory)
      .mockResolvedValueOnce({
        items: [{
          id: 'run-shared',
          status: 'succeeded',
          source: 'api',
          trigger_type: 'api',
          submitted_at: '2026-05-23T10:00:00Z',
          started_at: '2026-05-23T10:00:01Z',
          finished_at: '2026-05-23T10:00:02Z',
          latency_ms: 1000,
          error: null,
          task_type: 'deployment_invoke',
        }],
        next_cursor: 'next-cursor',
        limit: 50,
      })
      .mockResolvedValueOnce({
        items: [
          {
            id: 'run-shared',
            status: 'succeeded',
            source: 'api',
            trigger_type: 'api',
            submitted_at: '2026-05-23T10:00:00Z',
            started_at: '2026-05-23T10:00:01Z',
            finished_at: '2026-05-23T10:00:02Z',
            latency_ms: 1000,
            error: null,
            task_type: 'deployment_invoke',
          },
          {
            id: 'run-next',
            status: 'failed',
            source: 'webhook',
            trigger_type: 'webhook',
            submitted_at: '2026-05-24T10:00:00Z',
            started_at: '2026-05-24T10:00:01Z',
            finished_at: '2026-05-24T10:00:02Z',
            latency_ms: 1000,
            error: 'execution_failed',
            task_type: 'deployment_invoke',
          },
        ],
        next_cursor: null,
        limit: 50,
      });

    const user = userEvent.setup();
    renderAt(DEP_ID);
    await screen.findByRole('heading', { level: 1, name: 'API bot' });
    await user.click(screen.getByRole('tab', { name: /^Activity$/i }));
    await user.click(screen.getByRole('button', { name: /^Request records/ }));
    await user.click(await screen.findByRole('button', { name: 'Load older records' }));
    expect(await screen.findAllByText('run-shared')).toHaveLength(2);
    expect(await screen.findAllByText('run-next')).toHaveLength(2);

    await user.click(screen.getByRole('combobox', { name: 'Status' }));
    await user.click(screen.getByRole('option', { name: /failed/i }));
    await waitFor(() => {
      expect(getHistory).toHaveBeenCalledWith(DEP_ID, expect.objectContaining({
        status: ['failed'],
      }));
    });

    await user.click(screen.getByRole('combobox', { name: 'Sort' }));
    await user.click(screen.getByRole('option', { name: 'Oldest first' }));
    await waitFor(() => {
      expect(getHistory).toHaveBeenCalledWith(DEP_ID, expect.objectContaining({
        order: 'asc',
        status: ['failed'],
      }));
    });

    await user.click(screen.getByRole('combobox', { name: 'Time range' }));
    await user.click(screen.getByRole('option', { name: 'custom' }));
    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-05-20T08:00' } });
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '2026-05-25T18:00' } });
    await waitFor(() => {
      expect(getHistory).toHaveBeenCalledWith(DEP_ID, expect.objectContaining({
        from: new Date('2026-05-20T08:00').toISOString(),
        to: new Date('2026-05-25T18:00').toISOString(),
        order: 'asc',
        status: ['failed'],
      }));
    });
  });

  it('renders a recoverable history error and retries the query', async () => {
    vi.mocked(getHistory)
      .mockRejectedValueOnce(new Error('history unavailable'))
      .mockResolvedValueOnce({ items: [], next_cursor: null, limit: 50 });
    const user = userEvent.setup();
    renderAt(DEP_ID);
    await screen.findByRole('heading', { level: 1, name: 'API bot' });
    await user.click(screen.getByRole('tab', { name: /^Activity$/i }));
    await user.click(screen.getByRole('button', { name: /^Request records/ }));
    expect(await screen.findByText('Failed to load runs.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('No runs yet.')).toBeInTheDocument();
    expect(getHistory).toHaveBeenCalledTimes(2);
  });

  it('discards pending settings without changing the deployment', async () => {
    const user = userEvent.setup();
    renderAt(DEP_ID);
    await screen.findByRole('heading', { level: 1, name: 'API bot' });
    await user.click(screen.getByRole('tab', { name: /^Settings$/i }));
    const qps = screen.getByRole('spinbutton', { name: 'Rate limit (QPS)' });
    fireEvent.change(qps, { target: { value: '3' } });
    await user.click(screen.getByRole('switch', { name: 'Accept requests' }));
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: 'Discard changes' }));
    expect(qps).toHaveValue(10);
    expect(screen.getByRole('switch', { name: 'Accept requests' })).toBeChecked();
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled();
    expect(patchDeployment).not.toHaveBeenCalled();
  });

  it('queries the selected monitoring time range and aggregation', async () => {
    const user = userEvent.setup(); renderAt(DEP_ID);
    await screen.findByRole('heading', { name: 'Requests, errors, and latency' });
    await user.click(screen.getByRole('combobox', { name: 'Time range' }));
    await user.click(screen.getByRole('option', { name: 'Last 30 days' }));
    await waitFor(() => expect(getMetrics).toHaveBeenLastCalledWith(DEP_ID, expect.objectContaining({ bucket: 'minute' })));
    const params = vi.mocked(getMetrics).mock.calls.at(-1)![1];
    expect(Date.parse(params.to) - Date.parse(params.from)).toBe(30 * 86400000);
  });

  it('keeps minute buckets for short and fortnight monitoring ranges', async () => {
    const user = userEvent.setup(); renderAt(DEP_ID);
    await screen.findByRole('heading', { name: 'Requests, errors, and latency' });
    for (const [range, minutes] of [['5m', 5], ['10m', 10], ['30m', 30], ['1h', 60], ['3h', 180], ['14d', 20160]] as const) {
      await user.click(screen.getByRole('combobox', { name: 'Time range' }));
      await user.click(screen.getByRole('option', { name: `logs.range.${range}` }));
      await waitFor(() => {
        const params = vi.mocked(getMetrics).mock.calls.at(-1)![1];
        expect(params.bucket).toBe('minute');
        expect(Date.parse(params.to) - Date.parse(params.from)).toBe(minutes * 60000);
      });
    }
  });

  it('displays zero errors while keeping an all-zero chart finite', async () => {
    vi.mocked(getMetrics).mockResolvedValue({
      series: [{ ts: '2026-05-24T10:00:00Z', calls: 0, errors: 0, qps: 0, error_rate: 0, latency_p50: 0, latency_p95: 0 }],
      bucket: 'hour', from: '2026-05-23T00:00:00Z', to: '2026-05-24T11:00:00Z',
    });
    renderAt(DEP_ID);
    await screen.findByRole('heading', { level: 1, name: 'API bot' });
    const chart = await screen.findByRole('img', { name: 'deployments.metrics.errorRate; maximum 0 %' });
    expect(chart.querySelector('circle')).toHaveAttribute('cy', '162');
    expect(chart.innerHTML).not.toMatch(/NaN|Infinity/);
  });

  it('validates and saves the call timeout', async () => {
    const user = userEvent.setup(); renderAt(DEP_ID);
    await screen.findByRole('heading', { name: 'API bot' });
    await user.click(screen.getByRole('tab', { name: /^Settings$/i }));
    const input = screen.getByRole('spinbutton', { name: 'Call timeout (seconds)' });
    expect(input).toHaveValue(30);
    fireEvent.change(input, { target: { value: '0' } });
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled();
    fireEvent.change(input, { target: { value: '45' } });
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm and save' }));
    await waitFor(() => expect(patchDeployment).toHaveBeenCalledExactlyOnceWith(DEP_ID, { timeout_seconds: 45 }));
  });

  it('confirms a QPS-only update and preserves the draft on cancel', async () => {
    const user = userEvent.setup();
    renderAt(DEP_ID);
    await screen.findByRole('heading', { name: 'API bot' });
    await user.click(screen.getByRole('tab', { name: /^Settings$/i }));
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Rate limit (QPS)' }), { target: { value: '3' } });
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('10')).toBeInTheDocument();
    expect(within(dialog).getByText('3')).toBeInTheDocument();
    expect(within(dialog).getByText(/without restarting the instance/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/Prepare a new instance/)).not.toBeInTheDocument();
    expect(patchDeployment).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.getByRole('spinbutton', { name: 'Rate limit (QPS)' })).toHaveValue(3);
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    await user.click(screen.getByRole('button', { name: 'Confirm and save' }));
    await waitFor(() => expect(patchDeployment).toHaveBeenCalledExactlyOnceWith(DEP_ID, { rate_limit_qps: 3 }));
  });

  it('distinguishes the serving revision from a starting candidate', async () => {
    const dep = await getDeployment(DEP_ID);
    vi.mocked(getDeployment).mockResolvedValueOnce({ ...dep, rollout_status: 'preparing', active_revision_id: 'old-instance', runtime: { instances: [
      { id: 'old-instance', state: 'active', version: 'v1.sv0', mount_enabled: true, pending_requests: 2, created_at: dep.created_at, activated_at: dep.created_at },
      { id: 'new-instance', state: 'preparing', version: 'v1.sv1', mount_enabled: false, pending_requests: 0, created_at: dep.created_at, activated_at: null },
    ] } });
    renderAt(DEP_ID);
    await screen.findByText('Runtime instances');
    expect(screen.getByText('old-instance')).toBeInTheDocument();
    expect(screen.getByText('new-instance')).toBeInTheDocument();
    expect(screen.getAllByText('v1.sv0').length).toBeGreaterThan(0);
    for (const link of screen.getAllByRole('link', { name: 'v1.sv0' })) {
      expect(link).toHaveAttribute('href', `/preview?type=workflow&workflowId=wf_42&version=v1.sv0&instanceType=deployment&instanceId=${DEP_ID}&returnTo=` + encodeURIComponent(`/deployments/${DEP_ID}`));
    }
    expect(screen.queryByRole('link', { name: /View serving version/ })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'v1.sv1' })).toHaveAttribute('href', `/preview?type=workflow&workflowId=wf_42&version=v1.sv1&instanceType=deployment&instanceId=${DEP_ID}&returnTo=` + encodeURIComponent(`/deployments/${DEP_ID}`));
    expect(screen.getByText('v1.sv1')).toBeInTheDocument();
    expect(screen.getByText('Serving')).toBeInTheDocument();
    expect(screen.getByText('Starting')).toBeInTheDocument();
  });

  it('recommends CPU-based workers until manually overridden', async () => {
    const user = userEvent.setup();
    renderAt(DEP_ID);
    await screen.findByRole('heading', { name: 'API bot' });
    await user.click(screen.getByRole('tab', { name: /^Settings$/i }));
    await user.click(screen.getByText('Advanced · instance resources'));
    const cpu = screen.getByRole('spinbutton', { name: 'CPU cores' });
    const workers = screen.getByRole('spinbutton', { name: 'Worker processes' });
    const concurrency = screen.getByRole('spinbutton', { name: 'Concurrent executions per worker' });
    expect(workers).toHaveValue(1);
    expect(concurrency).toHaveValue(-1);
    fireEvent.change(cpu, { target: { value: '2.5' } });
    expect(workers).toHaveValue(2);
    fireEvent.change(workers, { target: { value: '3' } });
    fireEvent.change(cpu, { target: { value: '4' } });
    expect(workers).toHaveValue(3);
    fireEvent.change(concurrency, { target: { value: '2' } });
    expect(screen.getByText('Total concurrent executions: 6')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm and save' }));
    await waitFor(() => expect(patchDeployment).toHaveBeenCalledExactlyOnceWith(DEP_ID,
      { cpu_millis: 4000, worker_count: 3, worker_concurrency: 2 }));
  });

  it('confirms resource changes as an instance replacement', async () => {
    const user = userEvent.setup();
    renderAt(DEP_ID);
    await screen.findByRole('heading', { name: 'API bot' });
    await user.click(screen.getByRole('tab', { name: /^Settings$/i }));
    await user.click(screen.getByText('Advanced · instance resources'));
    fireEvent.change(screen.getByRole('spinbutton', { name: 'CPU cores' }), { target: { value: '1' } });
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Memory (MiB)' }), { target: { value: '1024' } });
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    const dialog = within(screen.getByRole('dialog'));
    expect(dialog.getByText('0.5')).toBeInTheDocument();
    expect(dialog.getByText('1024')).toBeInTheDocument();
    expect(dialog.getByText(/Prepare a new instance/)).toBeInTheDocument();
    expect(patchDeployment).not.toHaveBeenCalled();
    await user.click(dialog.getByRole('button', { name: 'Confirm and save' }));
    await waitFor(() => expect(patchDeployment).toHaveBeenCalledExactlyOnceWith(DEP_ID, { cpu_millis: 1000, memory_mb: 1024 }));
  });

  it.each([true, false])('explains mixed QPS and mount changes when enabled=%s', async (enabled) => {
    const user = userEvent.setup();
    renderAt(DEP_ID);
    await screen.findByRole('heading', { name: 'API bot' });
    await user.click(screen.getByRole('tab', { name: /^Settings$/i }));
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Rate limit (QPS)' }), { target: { value: '3' } });
    await user.click(screen.getByRole('switch', { name: 'Mount user storage (/mount)' }));
    if (!enabled) await user.click(screen.getByRole('switch', { name: 'Accept requests' }));
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    const dialog = within(screen.getByRole('dialog'));
    expect(dialog.getByText(/without restarting the instance/)).toBeInTheDocument();
    if (enabled) {
      expect(dialog.getByText(/If preparation fails, the current instance keeps serving/)).toBeInTheDocument();
    } else {
      expect(dialog.getByText(/Stop accepting new requests/)).toBeInTheDocument();
      expect(dialog.getByText(/No replacement instance is started/)).toBeInTheDocument();
      expect(dialog.queryByText(/Prepare a new instance/)).not.toBeInTheDocument();
    }
    expect(patchDeployment).not.toHaveBeenCalled();
  });

  it('confirms API-key rotation and requires acknowledging the one-time secret', async () => {
    const user = userEvent.setup();
    renderAt(DEP_ID);
    await screen.findByRole('heading', { level: 1, name: 'API bot' });
    await user.click(screen.getByRole('tab', { name: /^Settings$/i }));

    await user.click(screen.getByRole('button', { name: /rotate api key/i }));
    expect(screen.getByText(/current API key will stop working immediately/i)).toBeInTheDocument();
    expect(rotateKey).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: /^Cancel$/i }));
    expect(rotateKey).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: /rotate api key/i }));
    await user.click(screen.getAllByRole('button', { name: /rotate api key/i }).at(-1)!);
    expect(await screen.findByText('New API key')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /^Close$/i })[0]).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: /saved the new API key/i }));
    expect(screen.getAllByRole('button', { name: /^Close$/i })[0]).toBeEnabled();
  });

});
