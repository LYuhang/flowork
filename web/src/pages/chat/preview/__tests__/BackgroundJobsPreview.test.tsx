import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { PreviewOriginProvider } from '@/lib/preview/context-origin';
import { BackgroundJobsPreview } from '../BackgroundJobsPreview';

const mocks = vi.hoisted(() => ({
  referencedJob: vi.fn(),
  addContext: vi.fn().mockResolvedValue({}),
  fetchDraft: vi.fn().mockResolvedValue({}),
  cancelBackgroundJob: vi.fn().mockResolvedValue({}),
}));

vi.mock('@/lib/api/context-draft', () => ({ addContextToChat: mocks.addContext, fetchContextDraft: mocks.fetchDraft }));

vi.mock('@/lib/api/queries/chats', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/queries/chats')>(
    '@/lib/api/queries/chats',
  );
  return {
    ...actual,
    cancelBackgroundJob: mocks.cancelBackgroundJob,
    useReferencedBackgroundJob: mocks.referencedJob,
    useBackgroundJobs: () => ({
      data: [
        {
          job_id: 'job_live_1',
          chat_id: 'chat-1',
          runtime_type: 'codex',
          executor_type: 'runtime_task',
          tool_name: 'subagent',
          title: 'Research competitors',
          status: 'running',
          progress: { current: 2, total: 4, message: 'Reading pages' },
          input: { prompt: 'Compare three products' },
          result: {},
          result_ref: '/data/research/report.md',
          error: {},
          event_seq: 2,
          cancel_requested: false,
          delivery_status: 'pending',
          delivery_batch_id: null,
          created_at: '2026-07-25T10:00:00Z',
        },
      ],
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    }),
  };
});

vi.mock('@/lib/api/sse/background-job-events', () => ({
  useBackgroundJobEvents: () => undefined,
}));

describe('BackgroundJobsPreview', () => {
  beforeEach(() => mocks.referencedJob.mockReturnValue({data:null,isLoading:false,isError:false,refetch:vi.fn()}));
  it('opens a referenced task outside the recent list', () => {
    mocks.referencedJob.mockReturnValue({data:{job_id:'old-job', title:'Older task',status:'completed',progress:{},result:{},error:{},input:{},delivery_status:'delivered'},isLoading:false,isError:false});
    render(<QueryClientProvider client={new QueryClient()}>
      <BackgroundJobsPreview scopeId="scope-1" chatId="chat-1" initialJobId="old-job" />
    </QueryClientProvider>);
    expect(mocks.referencedJob).toHaveBeenCalledWith('scope-1','chat-1','old-job');
    expect(screen.getByText('Older task').closest('details')).toHaveAttribute('open');
    expect(screen.queryByText(/referenced task is unavailable/)).toBeNull();
  });
  it('does not misreport a transport error as a missing task', () => {
    mocks.referencedJob.mockReturnValue({data:null,isLoading:false,isError:true,refetch:vi.fn()});
    render(<QueryClientProvider client={new QueryClient()}>
      <BackgroundJobsPreview scopeId="scope-1" chatId="chat-1" initialJobId="old-job" />
    </QueryClientProvider>);
    expect(screen.getByText('Unable to load background tasks')).toBeInTheDocument();
    expect(screen.queryByText(/referenced task is unavailable/)).toBeNull();
  });
  it('explains when a directly referenced task is unavailable', () => {
    render(<QueryClientProvider client={new QueryClient()}>
      <BackgroundJobsPreview scopeId="scope-1" chatId="chat-1" initialJobId="missing" />
    </QueryClientProvider>);
    expect(screen.getByRole('status')).toHaveTextContent('The referenced task is unavailable');
  });
  it('quotes the right-clicked task without a permanent Quote button', async () => {
    render(<QueryClientProvider client={new QueryClient()}>
      <PreviewOriginProvider origin={{ chatId: 'chat-1' }}>
        <BackgroundJobsPreview scopeId="scope-1" chatId="chat-1" />
      </PreviewOriginProvider>
    </QueryClientProvider>);
    expect(screen.queryByText('Quote this task')).toBeNull();
    fireEvent.contextMenu(screen.getByText('Research competitors'));
    const item = await screen.findByRole('menuitem', { name: 'Quote this task' });
    await waitFor(() => expect(item).not.toHaveAttribute('aria-disabled', 'true'));
    fireEvent.click(item);
    await waitFor(() => expect(mocks.addContext).toHaveBeenCalledOnce());
    expect(mocks.addContext.mock.calls[0][0]).toBe('chat-1');
    expect(mocks.addContext.mock.calls[0][1][0].resource).toEqual({ kind: 'job', chat_id: 'chat-1', job_id: 'job_live_1' });
  });

  it('shows durable detail, opens result files, and confirms cancellation', () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const onOpenFile = vi.fn();
    render(
      <QueryClientProvider client={queryClient}>
        <BackgroundJobsPreview
          scopeId="scope-1"
          chatId="chat-1"
          initialJobId="job_live_1"
          onOpenFile={onOpenFile}
        />
      </QueryClientProvider>,
    );

    expect(screen.getByText('Research competitors')).toBeInTheDocument();
    expect(screen.getByText('Reading pages')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '/data/research/report.md' }));
    expect(onOpenFile).toHaveBeenCalledWith('/data/research/report.md');

    fireEvent.click(screen.getByRole('button', { name: 'Cancel job_live_1' }));
    expect(screen.getByText('Cancel this task?')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel task' })).toBeInTheDocument();
  });

  it('expands a collapsed task before showing its cancellation confirmation', () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <BackgroundJobsPreview
          scopeId="scope-1"
          chatId="chat-1"
        />
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Cancel job_live_1' }));

    expect(screen.getByText('Cancel this task?')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Cancel task' })).toBeVisible();
  });
});
