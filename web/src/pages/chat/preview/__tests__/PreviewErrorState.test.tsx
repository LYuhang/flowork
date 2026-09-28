import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import i18n from '@/lib/i18n';
import type { PreviewDescriptorV1 } from '@/lib/preview/protocol';
import { PreviewErrorState } from '../PreviewErrorState';

const descriptor: PreviewDescriptorV1 = {
  schemaVersion: 1,
  fileRef: {
    schemaVersion: 1,
    scope: 'project',
    projectId: 'chat-1',
    path: '/data/report.xlsx',
  },
  name: 'report.xlsx',
  sizeBytes: 1024,
  contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  detectedType: 'spreadsheet',
  revision: 'sha256:test',
  renderer: 'unsupported',
  loadPolicy: 'unsupported',
  capabilities: { preview: false, edit: false, download: true },
  content: {
    url: '/download/report.xlsx',
    truncated: false,
    rangeSupported: true,
  },
  error: {
    code: 'too_many_sheets',
    params: { actual: 37, limit: 20 },
  },
};

afterEach(async () => {
  await i18n.changeLanguage('en');
});

describe('PreviewErrorState', () => {
  it('renders a structured error and download action in English', async () => {
    await i18n.changeLanguage('en');
    render(<PreviewErrorState descriptor={descriptor} />);

    expect(screen.getByRole('alert')).toHaveAttribute(
      'data-preview-error',
      'too_many_sheets',
    );
    expect(screen.getByText(
      'This workbook contains 37 sheets. Preview supports up to 20.',
    )).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Download' })).toHaveAttribute(
      'href',
      '/download/report.xlsx',
    );
  });

  it('renders the same error contract in Chinese', async () => {
    await i18n.changeLanguage('zh');
    render(<PreviewErrorState descriptor={descriptor} />);

    expect(screen.getByText(
      '该工作簿包含 37 个 Sheet，Preview 最多支持 20 个。',
    )).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '下载' })).toBeInTheDocument();
  });

  it('uses a localized generic message for unknown error codes', async () => {
    await i18n.changeLanguage('zh');
    render(
      <PreviewErrorState
        descriptor={descriptor}
        error={{ code: 'future_error', params: {} }}
      />,
    );

    expect(screen.getByText('该文件暂时无法在 Preview 中展示。')).toBeInTheDocument();
  });
});
