import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import i18n from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CanvasExecutionHistory } from '../CanvasExecutionHistory';
import { HumanApprovalPanel } from '../nodes/HumanApprovalPanel';
import { sessionFetch } from '@/lib/api/session-fetch';
import { getWorkflowExecutionStatus } from '@/lib/api/executions';
import en from '@/lib/i18n/locales/en.json';

vi.mock('@/lib/api/session-fetch', () => ({ sessionFetch: vi.fn() }));
vi.mock('@/lib/api/executions', () => ({ getWorkflowExecutionStatus: vi.fn() }));
const request = vi.mocked(sessionFetch);
const current = vi.mocked(getWorkflowExecutionStatus);
let client: QueryClient;
let canDecide: boolean;
let submitted: boolean;
let workflowId: string;

async function show(enabled = true) {
  const instance = i18n.createInstance();
  await instance.use(initReactI18next).init({ lng: 'en', keySeparator: false, resources: { en: { translation: en } } });
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><I18nextProvider i18n={instance}>
    <CanvasExecutionHistory wfId="workflow" enabled={enabled}>
      <HumanApprovalPanel nodeId="review" instruction="Unsaved instruction" />
    </CanvasExecutionHistory>
  </I18nextProvider></QueryClientProvider>);
}

beforeEach(() => {
  canDecide = true;
  submitted = false;
  workflowId = 'workflow';
  current.mockReset();
  current.mockResolvedValue({ exec_id: 'execution', history_id: 'execution', wf_id: 'workflow', status: 'running' } as Awaited<ReturnType<typeof getWorkflowExecutionStatus>>);
  request.mockReset();
  request.mockImplementation(async (url, init) => {
    let result: unknown;
    if (init?.method === 'POST') {
      submitted = true;
      result = {};
    } else if (String(url).includes('/events?')) {
      result = { events: [], last_seq: 0, status: 'waiting_approval' };
    } else {
      result = {
        id: 'execution', wf_id: workflowId, status: 'waiting_approval',
        server_time: '2020-01-01T00:00:00Z',
        workflow: { review: { node_config: { instruction: 'Saved approval instruction' } } },
        approvals: [{ id: 'approval', node_id: 'review', status: submitted ? 'decision_requested' : 'pending',
          deadline: '2020-01-01T00:01:00Z', approved: null, can_decide: canDecide }],
      };
    }
    return new Response(JSON.stringify(result), { headers: { 'Content-Type': 'application/json' } });
  });
});
afterEach(() => { cleanup(); client?.clear(); });

describe('Canvas approval restoration', () => {
  it.each([true, false])('submits approved=%s for the saved invocation', async (approved) => {
    const user = userEvent.setup();
    await show();
    expect(await screen.findByText('Saved approval instruction')).toBeInTheDocument();
    expect(screen.queryByText('Unsaved instruction')).not.toBeInTheDocument();
    // Countdown uses server time even when the local clock is years ahead.
    expect(screen.getByRole('timer')).toHaveTextContent('00:01:00');
    await user.click(screen.getByRole('button', { name: approved ? 'Approve' : 'Reject' }));
    await waitFor(() => expect(request.mock.calls.some(([url, init]) =>
      String(url).endsWith('/execution/approvals/approval') && init?.body === JSON.stringify({ approved }),
    )).toBe(true));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument());
  });

  it('prevents non-assignees from submitting a decision', async () => {
    canDecide = false;
    await show();
    expect(await screen.findByRole('button', { name: 'Approve' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Reject' })).toBeDisabled();
  });

  it('never displays another workflow’s approval', async () => {
    workflowId = 'another-workflow';
    await show();
    await waitFor(() => expect(client.getQueryData(['workflow-execution', 'execution'])).toBeDefined());
    expect(screen.queryByText('Saved approval instruction')).not.toBeInTheDocument();
  });

  it('does not request history without inspect permission', async () => {
    await show(false);
    expect(current).not.toHaveBeenCalled();
    expect(request).not.toHaveBeenCalled();
  });
});
