import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { PreviewDescriptorV1 } from '@/lib/preview/protocol';
import { ChatFilePreview } from '../ChatFilePreview';

const descriptor = {
  schemaVersion: 1,
  fileRef: {
    schemaVersion: 1,
    scope: 'chat',
    chatId: 'chat-1',
    path: '/data/brief.pdf',
  },
  name: 'brief.pdf',
  sizeBytes: 128,
  contentType: 'application/pdf',
  detectedType: 'pdf',
  revision: 'sha256:brief',
  renderer: 'unsupported',
  loadPolicy: 'inline',
  capabilities: { preview: true, edit: false, download: true },
  content: {
    url: '/api/v1/previews/content/brief.pdf',
    truncated: false,
    rangeSupported: true,
  },
} satisfies PreviewDescriptorV1;

vi.mock('@/lib/api/queries/previews', () => ({
  usePreviewDescriptor: () => ({
    data: descriptor,
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  }),
}));

afterEach(cleanup);

describe('ChatFilePreview toolbar', () => {
  it('uses the resolved resource for standalone navigation after a legacy citation fallback', () => {
    render(<ChatFilePreview fileRef={{ ...descriptor.fileRef, path: '/data/brief.pdf:24' }} />);
    const href = screen.getByRole('link', { name: 'Open in new page' }).getAttribute('href');
    expect(href).toContain('path=%2Fdata%2Fbrief.pdf&');
    expect(href).not.toContain('%3A24');
  });

  it('opens the same authorized file coordinates in a standalone page', () => {
    render(<ChatFilePreview fileRef={descriptor.fileRef} />);

    const link = screen.getByRole('link', { name: 'Open in new page' });
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'));
    expect(link.getAttribute('href')).toContain(
      '/preview?scope=chat&path=%2Fdata%2Fbrief.pdf&fileType=auto&chatId=chat-1',
    );
    expect(link.getAttribute('href')).not.toMatch(/token|credential|authorization/i);
  });

  it('does not repeat the action inside the standalone Preview page', () => {
    render(
      <ChatFilePreview
        fileRef={descriptor.fileRef}
        allowOpenInNewPage={false}
      />,
    );

    expect(screen.queryByRole('link', { name: 'Open in new page' })).not.toBeInTheDocument();
  });
});
