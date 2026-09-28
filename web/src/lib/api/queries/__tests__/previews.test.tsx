import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  previewDescriptorQueryKey,
  usePreviewDescriptor,
  useWritePreviewFile,
} from '@/lib/api/queries/previews';
import type {
  ProjectFileRefV1,
  PreviewDescriptorV1,
} from '@/lib/preview/protocol';
import { useAuthStore } from '@/stores/auth';

const mocks = vi.hoisted(() => ({
  fetchEventSource: vi.fn(),
  resolvePreview: vi.fn(),
  writePreviewFile: vi.fn(),
}));

vi.mock('@microsoft/fetch-event-source', () => ({
  fetchEventSource: mocks.fetchEventSource,
}));

vi.mock('@/lib/api/previews', () => ({
  resolvePreview: mocks.resolvePreview,
  writePreviewFile: mocks.writePreviewFile,
}));

const fileRef: ProjectFileRefV1 = {
  schemaVersion: 1,
  scope: 'project',
  projectId: 'chat-preview-events',
  path: '/data/notes.md',
};

function descriptor(
  revision: string,
  content = 'before',
): PreviewDescriptorV1 {
  return {
    schemaVersion: 1,
    fileRef,
    name: 'notes.md',
    sizeBytes: content.length,
    contentType: 'text/markdown',
    detectedType: 'markdown',
    revision,
    renderer: 'markdown',
    loadPolicy: 'inline',
    capabilities: { preview: true, edit: true, download: true },
    content: {
      inlineText: content,
      truncated: false,
      rangeSupported: false,
    },
    text: {
      encoding: 'utf-8',
      bom: false,
      newline: 'LF',
      mixedNewlines: false,
    },
  };
}

function createWrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        {children}
      </QueryClientProvider>
    );
  };
}

describe('Preview descriptor live updates', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({ token: null, authenticated: true });
    mocks.fetchEventSource.mockResolvedValue(undefined);
  });

  it('does not poll and ignores the current revision, but refetches a remote revision', async () => {
    const first = descriptor('sha256:first', 'before');
    const second = descriptor('sha256:second', 'agent update');
    mocks.resolvePreview
      .mockResolvedValueOnce(first)
      .mockResolvedValue(second);
    let streamOptions: {
      onmessage: (message: {
        event: string;
        data: string;
      }) => void;
    } | undefined;
    mocks.fetchEventSource.mockImplementation(
      async (_url: string, options: typeof streamOptions) => {
        streamOptions = options;
      },
    );
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const { result } = renderHook(
      () => usePreviewDescriptor(fileRef),
      { wrapper: createWrapper(queryClient) },
    );
    await waitFor(() => expect(result.current.data?.revision).toBe('sha256:first'));
    expect(mocks.resolvePreview).toHaveBeenCalledTimes(1);
    expect(streamOptions).toBeDefined();

    act(() => {
      streamOptions?.onmessage({
        event: 'preview_ready',
        data: JSON.stringify({
          event_id: 10,
          path: fileRef.path,
          revision: 'sha256:first',
        }),
      });
      streamOptions?.onmessage({
        event: 'preview_file',
        data: JSON.stringify({
          event_id: 11,
          path: fileRef.path,
          changed_path: fileRef.path,
          event_type: 'upsert',
          revision: 'sha256:first',
          derived: false,
        }),
      });
    });
    await Promise.resolve();
    expect(mocks.resolvePreview).toHaveBeenCalledTimes(1);

    act(() => {
      streamOptions?.onmessage({
        event: 'preview_file',
        data: JSON.stringify({
          event_id: 12,
          path: fileRef.path,
          changed_path: fileRef.path,
          event_type: 'upsert',
          revision: 'sha256:second',
          derived: false,
        }),
      });
    });
    await waitFor(() => expect(result.current.data?.revision).toBe('sha256:second'));
    expect(mocks.resolvePreview).toHaveBeenCalledTimes(2);

    // Advancing well beyond the removed 3-second interval causes no request.
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(mocks.resolvePreview).toHaveBeenCalledTimes(2);
  });

  it('shares one live event transport across duplicate views of the same file', async () => {
    mocks.resolvePreview.mockResolvedValue(descriptor('sha256:first'));
    let streamSignal: AbortSignal | undefined;
    mocks.fetchEventSource.mockImplementation(
      async (_url: string, options: { signal?: AbortSignal }) => {
        streamSignal = options.signal;
        await new Promise<void>(() => {});
      },
    );
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const first = renderHook(
      () => usePreviewDescriptor(fileRef),
      { wrapper: createWrapper(queryClient) },
    );
    const second = renderHook(
      () => usePreviewDescriptor({ ...fileRef }),
      { wrapper: createWrapper(queryClient) },
    );

    await waitFor(() => expect(mocks.fetchEventSource).toHaveBeenCalledTimes(1));
    expect(streamSignal?.aborted).toBe(false);

    first.unmount();
    expect(streamSignal?.aborted).toBe(false);
    second.unmount();
    expect(streamSignal?.aborted).toBe(true);
  });

  it('installs an explicit Save result locally without resolving again', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    queryClient.setQueryData(
      previewDescriptorQueryKey(fileRef),
      descriptor('sha256:first', 'before'),
    );
    mocks.writePreviewFile.mockResolvedValue({
      fileRef,
      revision: 'sha256:saved',
      sizeBytes: 5,
      contentType: 'text/markdown',
    });
    const { result } = renderHook(
      () => useWritePreviewFile(fileRef),
      { wrapper: createWrapper(queryClient) },
    );
    await act(async () => {
      await result.current.mutateAsync({
        expectedRevision: 'sha256:first',
        contentType: 'text/markdown',
        content: 'after',
      });
    });
    expect(queryClient.getQueryData<PreviewDescriptorV1>(
      previewDescriptorQueryKey(fileRef),
    )).toMatchObject({
      revision: 'sha256:saved',
      sizeBytes: 5,
      content: { inlineText: 'after' },
    });
    expect(mocks.resolvePreview).not.toHaveBeenCalled();
  });
});
