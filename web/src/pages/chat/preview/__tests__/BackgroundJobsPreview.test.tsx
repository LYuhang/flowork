import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { BackgroundJobsPreview } from '../BackgroundJobsPreview';

const mocks = vi.hoisted(() => ({
  cancelBackgroundJob: vi.fn().mockResolvedValue({}),
}));

vi.mock('@/lib/api/queries/chats', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/queries/chats')>(
    '@/lib/api/queries/chats',
  );
  return {
    ...actual,
    cancelBackgroundJob: mocks.cancelBackgroundJob,
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
