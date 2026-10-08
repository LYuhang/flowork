import { TooltipProvider } from '@/components/ui/tooltip';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import i18n from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ExecutionHistory } from '../execution-history';
import { sessionFetch } from '@/lib/api/session-fetch';
import en from '@/lib/i18n/locales/en.json';
import zh from '@/lib/i18n/locales/zh.json';

vi.mock('@/lib/api/session-fetch', () => ({ sessionFetch: vi.fn() }));
vi.mock('@/lib/timezone', () => ({ useFormatDateTime: () => (value: string) => value }));
const request = vi.mocked(sessionFetch);
const first = { id: 'first', status: 'waiting_approval', input_index: 0, created_at: '2026-10-01T12:00:00Z' };
const second = { id: 'second', status: 'succeeded', input_index: 1, created_at: '2026-10-01T11:00:00Z' };
let client: QueryClient;

async function show(locale = 'en') {
  const instance = i18n.createInstance();
  await instance.use(initReactI18next).init({
    lng: locale, fallbackLng: 'en', keySeparator: false, resources: { en: { translation: en }, zh: { translation: zh } },
  });
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><I18nextProvider i18n={instance}><MemoryRouter><TooltipProvider>
    <ExecutionHistory source="task" sourceId="task-review" />
  </TooltipProvider></MemoryRouter></I18nextProvider></QueryClientProvider>);
}

beforeEach(() => {
  request.mockReset();
  request.mockImplementation(async (url) => {
    const params = new URL(String(url), 'https://flowork.test').searchParams;
    const page = params.has('before_id') ? { items: [second], has_more: false }
      : params.get('mine') === 'true' ? { items: [first], has_more: false }
        : params.get('statuses') === 'failed' ? { items: [], has_more: false }
          : { items: [first, first], has_more: true };
    return new Response(JSON.stringify(page), { status: 200, headers: { 'Content-Type': 'application/json' } });
  });
});
afterEach(() => { cleanup(); client?.clear(); });

describe('Execution history', () => {
  it('keeps loaded rows and the scroll container after a failed refresh, then recovers', async () => {
    await show();
    const link = await screen.findByRole('link', { name: 'Execution details' });
    const table = screen.getByRole('table');
    const scroll = table.parentElement!;
    scroll.scrollTop = 120;
    request.mockRejectedValue(new TypeError('network unavailable'));
    await userEvent.click(screen.getByRole('button', { name: 'Refresh executions' }));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Execution details' })).toBe(link);
    expect(screen.getByRole('table').parentElement).toBe(scroll);
    expect(scroll.scrollTop).toBe(120);
    request.mockResolvedValue(new Response(JSON.stringify({ items: [first], has_more: false })));
    await userEvent.click(screen.getByRole('button', { name: 'Refresh executions' }));
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
    expect(screen.getByRole('link', { name: 'Execution details' })).toBe(link);
  });

  it.each([401, 403, 404])('removes revoked history on HTTP %s and does not restore it after a network error', async status => {
    await show();
    await screen.findByRole('link', { name: 'Execution details' });
    request.mockImplementation(async () => new Response('{}', { status }));
    await userEvent.click(screen.getByRole('button', { name: 'Refresh executions' }));
    await screen.findByRole('alert');
    expect(screen.queryByRole('link', { name: 'Execution details' })).toBeNull();
    request.mockRejectedValue(new TypeError('network unavailable'));
    await userEvent.click(screen.getByRole('button', { name: 'Refresh executions' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh executions' })).toBeEnabled());
    expect(screen.queryByRole('link', { name: 'Execution details' })).toBeNull();
  });

  it('shows the reviewer and a countdown relative to server time', async () => {
    request.mockResolvedValue(new Response(JSON.stringify({
      items: [{ ...first, pending_approvals: [{
        id: 'approval', node_id: 'node_2', approver_email: 'reviewer@example.com',
        deadline: '2026-10-01T12:02:00Z',
      }] }],
      has_more: false, server_time: '2026-10-01T12:00:00Z',
    })));
    await show();
    expect(await screen.findByText(/reviewer@example.com/)).toBeInTheDocument();
    expect(screen.getByRole('timer')).toHaveTextContent('00:02:00');
  });

  it('shows one detail link per input, pages by cursor, and applies server-side approval/status filters', async () => {
    const user = userEvent.setup();
    await show();
    const link = await screen.findByRole('link', { name: 'Execution details' });
    expect(link).toHaveAttribute('href', '/workflow-executions/first');
    expect(screen.getByText('Input 1')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Load earlier executions' }));
    await waitFor(() => expect(screen.getAllByRole('link', { name: 'Execution details' })).toHaveLength(2));
    const cursor = new URL(String(request.mock.calls.at(-1)?.[0]), 'https://flowork.test').searchParams;
    expect(cursor.get('before_id')).toBe('first');
    expect(cursor.get('before_time')).toBe(first.created_at);
    await user.click(screen.getByRole('combobox', { name: 'Filter execution status' }));
    await user.click(screen.getByRole('option', { name: 'Awaiting my review' }));
    await waitFor(() => expect(screen.getAllByRole('link', { name: 'Execution details' })).toHaveLength(1));
    const mine = new URL(String(request.mock.calls.at(-1)?.[0]), 'https://flowork.test').searchParams;
    expect(mine.get('source_type')).toBe('task');
    expect(mine.get('source_id')).toBe('task-review');
    expect(mine.get('mine')).toBe('true');
    expect(mine.has('before_id')).toBe(false);
    await user.click(screen.getByRole('combobox', { name: 'Filter execution status' }));
    await user.click(screen.getByRole('option', { name: 'Failed' }));
    expect(await screen.findByText('No matching executions.')).toBeInTheDocument();
    expect(new URL(String(request.mock.calls.at(-1)?.[0]), 'https://flowork.test').searchParams.get('statuses')).toBe('failed');
  });

  it('renders review status and navigation in Chinese', async () => {
    await show('zh');
    expect(await screen.findByRole('link', { name: '执行详情' })).toBeInTheDocument();
    expect(screen.getByText('第 1 条输入')).toBeInTheDocument();
    expect(screen.getByText('待确认')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: '筛选执行状态' })).toBeInTheDocument();
  });
});
