import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { describe, expect, it, vi } from 'vitest';

import { StandalonePreviewPage } from '@/pages/preview/StandalonePreviewPage';

vi.mock('@/pages/chat/preview/WorkflowPreviewRenderer', () => ({
  WorkflowPreviewRenderer: (props: Record<string, unknown>) => <div data-testid="shared-workflow-preview">{JSON.stringify(props)}</div>,
}));

vi.mock('@/pages/chat/preview/ChatFilePreview', () => ({
  ChatFilePreview: ({
    fileRef,
    fileType,
    allowOpenInNewPage,
  }: {
    fileRef: { path: string; chatId?: string };
    fileType: string;
    allowOpenInNewPage?: boolean;
  }) => (
    <div data-testid="shared-preview">
      {fileRef.chatId}:{fileRef.path}:{fileType}:{String(allowOpenInNewPage)}
    </div>
  ),
}));

describe('StandalonePreviewPage', () => {
  it('opens the exact workflow snapshot with a right Inspector and no recursive maximize', async () => {
    render(<MemoryRouter initialEntries={['/preview?type=workflow&workflowId=wf&version=v2.sv3']}><StandalonePreviewPage /></MemoryRouter>);
    const renderer = await screen.findByTestId('shared-workflow-preview');
    expect(renderer).toHaveTextContent('"version":"v2.sv3"');
    expect(renderer).toHaveTextContent('"inspectorPlacement":"right"');
    expect(renderer).toHaveTextContent('"allowOpenInNewPage":false');
    expect(screen.queryByTestId('shared-preview')).not.toBeInTheDocument();
  });

  it('rejects an unversioned workflow rather than displaying latest', () => {
    render(<MemoryRouter initialEntries={['/preview?type=workflow&workflowId=wf']}><StandalonePreviewPage /></MemoryRouter>);
    expect(screen.getByText('Unable to open Preview')).toBeInTheDocument();
    expect(screen.queryByTestId('shared-workflow-preview')).not.toBeInTheDocument();
  });
  it('reuses the shared file Preview for a validated URL', () => {
    render(
      <MemoryRouter initialEntries={[
        '/preview?scope=chat&chatId=chat-1&path=%2Fdata%2Fbrief.docx&fileType=docx',
      ]}>
        <StandalonePreviewPage />
      </MemoryRouter>,
    );

    expect(screen.getByRole('heading', { name: 'brief.docx' })).toBeInTheDocument();
    expect(screen.getByTestId('shared-preview')).toHaveTextContent(
      'chat-1:/data/brief.docx:docx:false',
    );
  });

  it('does not call the Preview renderer for an invalid file coordinate', () => {
    render(
      <MemoryRouter initialEntries={[
        '/preview?scope=chat&chatId=chat-1&path=%2Fetc%2Fpasswd',
      ]}>
        <StandalonePreviewPage />
      </MemoryRouter>,
    );

    expect(screen.getByText('Unable to open Preview')).toBeInTheDocument();
    expect(screen.queryByTestId('shared-preview')).not.toBeInTheDocument();
  });
});
