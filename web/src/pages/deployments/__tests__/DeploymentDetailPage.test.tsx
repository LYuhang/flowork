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
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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
    version_pin: 'head',
    pinned_major: null,
    pinned_sub: null,
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
  useWorkflow: () => ({
    data: {
      workflow: {
        node_1: {
          node_type: 'StartNode',
          input_fields: {
            analysis_focus: { type: 'string' },
          },
        },
      },
    },
  }),
}));

import { DeploymentDetailPage } from '@/pages/deployments/DeploymentDetailPage';
import { getHistory, getMetrics, patchDeployment, rotateKey } from '@/lib/api/deployments';

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: {} } },
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

    // Overview is the default and owns editable basic information.
    expect(screen.getAllByText('Active').length).toBeGreaterThan(0);
    expect(screen.getByText('Basic information')).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Name' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Edit' }));
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveValue('API bot');
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('textbox', { name: 'Name' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('tab', { name: /^Usage$/i }));
    expect(screen.getAllByText(/deployments\/bot\/invoke/).length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: /copy endpoint/i })).toBeInTheDocument();

    // Settings keeps high-risk API key rotation explicit.
    await user.click(screen.getByRole('tab', { name: /^Settings$/i }));
    expect(screen.getByText('Traffic and runtime controls')).toBeInTheDocument();
    expect(screen.queryByText('Basic information')).not.toBeInTheDocument();
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

  it('displays zero errors while keeping an all-zero chart finite', async () => {
    vi.mocked(getMetrics).mockResolvedValue({
      series: [{ ts: '2026-05-24T10:00:00Z', calls: 0, errors: 0, latency_p50: 0, latency_p95: 0 }],
      bucket: 'hour', from: '2026-05-23T00:00:00Z', to: '2026-05-24T00:00:00Z',
    });
    const user = userEvent.setup();
    renderAt(DEP_ID);
    await screen.findByRole('heading', { level: 1, name: 'API bot' });
    await user.click(screen.getByRole('tab', { name: /^Activity$/i }));
    const chart = await screen.findByRole('img', { name: 'Errors; maximum 0 errors' });
    expect(chart.querySelector('circle')).toHaveAttribute('cy', '44');
    expect(chart.innerHTML).not.toMatch(/NaN|Infinity/);
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
