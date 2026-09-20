import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';
import { server } from '@/__tests__/msw-handlers';
import { VfsFilePreviewContent } from '../VfsFilePreviewContent';
import type { VfsReadOut } from '@/lib/api/vfs';
import type { FileRefV1, PreviewDescriptorV1 } from '@/lib/preview/protocol';
import i18n from '@/lib/i18n';
import { useAuthStore } from '@/stores/auth';

beforeEach(async () => {
  await i18n.changeLanguage('en');
  // No live SSE subscription in this descriptor/rendering test.
  useAuthStore.setState({ authenticated: false });
});

const binary: VfsReadOut = {
  path: '/mount/preview.png', content_type: 'image/png', content: null,
  size_bytes: 101935, truncated: false, stale: false,
};

function show(data: VfsReadOut, runId?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <VfsFilePreviewContent data={data} runId={runId} loading={false} error={false} onRetry={() => {}} />
    </QueryClientProvider>,
  );
}

describe('canvas binary file preview', () => {
  it.each([
    { path: '/mount/preview.png', scope: 'mount', runId: undefined },
    { path: '/run/preview.png', scope: 'run', runId: 'run-1' },
  ])('resolves real media for $scope instead of treating null as missing file', async ({ path, scope, runId }) => {
    let received: FileRefV1 | undefined;
    server.use(http.post('*/api/v1/previews/resolve', async ({ request }) => {
      const body = await request.json() as { fileRef: FileRefV1 };
      received = body.fileRef;
      return HttpResponse.json({
        schemaVersion: 1, fileRef: body.fileRef, name: 'preview.png',
        sizeBytes: 101935, contentType: 'image/png', detectedType: 'image',
        revision: 'r1', renderer: 'image', loadPolicy: 'inline',
        capabilities: { preview: true, edit: false, download: true },
        content: { url: '/api/v1/previews/content/test-image', truncated: false, rangeSupported: true },
      } satisfies PreviewDescriptorV1);
    }));
    show({ ...binary, path }, runId);
    const image = await screen.findByRole('img', { name: 'preview.png' });
    expect(image).toHaveAttribute('src', '/api/v1/previews/content/test-image');
    expect(received).toEqual({ schemaVersion: 1, scope, path, ...(runId ? { runId } : {}) });
    expect(screen.queryByText('This file has no text preview.')).toBeNull();
    expect(screen.getByRole('link', { name: 'Download' })).toBeInTheDocument();
  });

  it('contains descriptor failures inside the preview and offers retry', async () => {
    server.use(http.post('*/api/v1/previews/resolve', () =>
      HttpResponse.json({ detail: 'File not found' }, { status: 404 })));
    show(binary);
    expect(await screen.findByText('Unable to open this file')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
  });

  it('continues rendering text files without resolving a media descriptor', () => {
    show({ ...binary, path: '/mount/notes.txt', content_type: 'text/plain', content: 'Existing text' });
    expect(screen.getByText('Existing text')).toBeInTheDocument();
  });
});
