import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { PreviewDescriptorV1 } from '@/lib/preview/protocol';
import { SpreadsheetPreviewRenderer } from '../SpreadsheetPreviewRenderer';

const nativeViewerProps = vi.hoisted(() => vi.fn());

vi.mock('@file-viewer/react', () => ({
  default: (props: Record<string, unknown>) => {
    nativeViewerProps(props);
    return <div data-testid="native-file-viewer">Native workbook</div>;
  },
}));
vi.mock('@file-viewer/renderer-spreadsheet', () => ({
  spreadsheetRenderer: { name: 'spreadsheet' },
}));
vi.mock('next-themes', () => ({
  useTheme: () => ({ resolvedTheme: 'light' }),
}));
vi.mock('ag-grid-community', () => ({
  AllCommunityModule: {},
  ModuleRegistry: { registerModules: vi.fn() },
  themeQuartz: { withParams: vi.fn(() => ({})) },
}));
vi.mock('ag-grid-react', () => ({
  AgGridReact: () => <div data-testid="structured-data-grid" />,
}));

const descriptor: PreviewDescriptorV1 = {
  schemaVersion: 1,
  fileRef: {
    schemaVersion: 1,
    scope: 'project',
    projectId: 'chat-1',
    path: '/data/pending.csv',
  },
  name: 'pending.csv',
  sizeBytes: 128,
  contentType: 'text/csv',
  detectedType: 'csv',
  revision: 'sha256:pending',
  renderer: 'spreadsheet',
  loadPolicy: 'stream',
  capabilities: { preview: true, edit: false, download: true },
  content: {
    url: '/api/v1/previews/content/pending.csv',
    truncated: false,
    rangeSupported: true,
  },
};

afterEach(() => {
  vi.unstubAllGlobals();
  nativeViewerProps.mockReset();
});

describe('SpreadsheetPreviewRenderer lifecycle', () => {
  it('opens native workbooks in one style-preserving spreadsheet viewer', async () => {
    const view = render(
      <SpreadsheetPreviewRenderer
        descriptor={{
          ...descriptor,
          name: 'board.xlsx',
          fileRef: {
            schemaVersion: 1,
            scope: 'project',
            projectId: 'chat-1',
            path: '/data/board.xlsx',
          },
          detectedType: 'spreadsheet',
          contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          rendition: null,
        }}
        loadAllowed
        onDirtyChange={() => undefined}
      />,
    );

    expect(await screen.findByTestId('native-file-viewer')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Formatted view' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Data view' })).not.toBeInTheDocument();
    expect(nativeViewerProps).toHaveBeenCalledWith(expect.objectContaining({
      url: '/api/v1/previews/content/pending.csv',
      name: 'board.xlsx',
      type: 'xlsx',
      options: expect.objectContaining({
        rendererMode: 'replace',
        toolbar: expect.objectContaining({ download: false }),
        spreadsheet: expect.objectContaining({
          resizableColumns: true,
          resizableRows: true,
        }),
      }),
    }));
    const firstProps = nativeViewerProps.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    view.rerender(
      <SpreadsheetPreviewRenderer
        descriptor={{
          ...descriptor,
          name: 'board.xlsx',
          fileRef: {
            schemaVersion: 1,
            scope: 'project',
            projectId: 'chat-1',
            path: '/data/board.xlsx',
          },
          detectedType: 'spreadsheet',
          contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          rendition: null,
        }}
        loadAllowed
        onDirtyChange={() => undefined}
      />,
    );
    const rerenderedProps = nativeViewerProps.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    expect(rerenderedProps.onStateChange).toBe(firstProps.onStateChange);
    expect(rerenderedProps.options).toBe(firstProps.options);
  });

  it('keeps structured text tables read-only even if a stale descriptor advertises edit', async () => {
    render(
      <SpreadsheetPreviewRenderer
        descriptor={{
          ...descriptor,
          loadPolicy: 'inline',
          content: {
            inlineText: 'name,value\nalpha,1\n',
            truncated: false,
            rangeSupported: false,
          },
        }}
        loadAllowed
        onDirtyChange={() => undefined}
      />,
    );

    expect(await screen.findByText('Read only')).toBeInTheDocument();
    expect(screen.getByTestId('structured-data-grid')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
  });

  it('aborts an in-flight structured-table read when its Preview tab is closed', async () => {
    let requestSignal: AbortSignal | undefined;
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      requestSignal = init?.signal ?? undefined;
      return new Promise<Response>((_resolve, reject) => {
        requestSignal?.addEventListener(
          'abort',
          () => reject(new DOMException('Aborted', 'AbortError')),
          { once: true },
        );
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });

    const view = render(
      <QueryClientProvider client={queryClient}>
        <SpreadsheetPreviewRenderer
          descriptor={descriptor}
          loadAllowed
          onDirtyChange={() => undefined}
        />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    expect(requestSignal?.aborted).toBe(false);

    view.unmount();

    expect(requestSignal?.aborted).toBe(true);
  });

  it('does not reload an unchanged structured table when its parent rerenders', async () => {
    const fetchMock = vi.fn(async () => new Response('name,value\nalpha,1\n'));
    vi.stubGlobal('fetch', fetchMock);
    const view = render(
      <SpreadsheetPreviewRenderer
        descriptor={descriptor}
        loadAllowed
        onDirtyChange={() => undefined}
      />,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    await screen.findByText('Read only');

    view.rerender(
      <SpreadsheetPreviewRenderer
        descriptor={{
          ...descriptor,
          fileRef: { ...descriptor.fileRef },
          content: {
            inlineText: descriptor.content?.inlineText,
            url: descriptor.content?.url,
            truncated: descriptor.content?.truncated ?? false,
            rangeSupported: descriptor.content?.rangeSupported ?? false,
          },
        }}
        loadAllowed
        onDirtyChange={() => undefined}
      />,
    );
    await Promise.resolve();
    expect(fetchMock).toHaveBeenCalledOnce();
  });
});
