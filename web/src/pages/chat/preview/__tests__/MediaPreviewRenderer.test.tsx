import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/lib/i18n';
import type { PreviewDescriptorV1 } from '@/lib/preview/protocol';
import { MediaPreviewRenderer } from '../MediaPreviewRenderer';

const imageDescriptor: PreviewDescriptorV1 = {
  schemaVersion: 1,
  fileRef: {
    schemaVersion: 1,
    scope: 'project',
    projectId: 'chat-1',
    path: '/data/chart.png',
  },
  name: 'chart.png',
  sizeBytes: 1024,
  contentType: 'image/png',
  detectedType: 'image',
  revision: 'sha256:image',
  renderer: 'image',
  loadPolicy: 'range',
  capabilities: { preview: true, edit: false, download: true },
  content: {
    url: '/api/v1/previews/content/chart.png',
    truncated: false,
    rangeSupported: true,
  },
};

afterEach(async () => {
  await i18n.changeLanguage('en');
});

describe('MediaPreviewRenderer', () => {
  it('provides bounded image zoom and reset controls', async () => {
    await i18n.changeLanguage('en');
    render(
      <MediaPreviewRenderer
        descriptor={imageDescriptor}
        loadAllowed
        onDirtyChange={() => undefined}
      />,
    );

    const image = screen.getByRole('img', { name: 'chart.png' });
    expect(screen.getByText('100%')).toBeInTheDocument();
    expect(image).toHaveStyle({ transform: 'scale(1)' });

    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    expect(screen.getByText('125%')).toBeInTheDocument();
    expect(image).toHaveStyle({ transform: 'scale(1.25)' });

    fireEvent.click(screen.getByRole('button', { name: 'Reset zoom' }));
    expect(screen.getByText('100%')).toBeInTheDocument();
    expect(image).toHaveStyle({ transform: 'scale(1)' });
  });

  it('does not add image controls to audio previews', () => {
    render(
      <MediaPreviewRenderer
        descriptor={{
          ...imageDescriptor,
          name: 'sample.mp3',
          contentType: 'audio/mpeg',
          detectedType: 'audio',
          renderer: 'audio',
        }}
        loadAllowed
        onDirtyChange={() => undefined}
      />,
    );

    expect(screen.queryByText('100%', { exact: true })).not.toBeInTheDocument();
    expect(screen.queryByRole('toolbar', { name: 'Image zoom controls' })).not.toBeInTheDocument();
  });

  it('refreshes an expired media URL once before showing a renderer error', () => {
    const onReload = vi.fn();
    render(
      <MediaPreviewRenderer
        descriptor={imageDescriptor}
        loadAllowed
        onDirtyChange={() => undefined}
        onReload={onReload}
      />,
    );

    const image = screen.getByRole('img', { name: 'chart.png' });
    fireEvent.error(image);
    expect(onReload).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('Preview unavailable')).not.toBeInTheDocument();

    fireEvent.error(image);
    expect(onReload).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Preview unavailable')).toBeInTheDocument();
  });
});
