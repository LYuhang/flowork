import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router';
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
    fileRef: { path: string; projectId?: string };
    fileType: string;
    allowOpenInNewPage?: boolean;
  }) => (
    <div data-testid="shared-preview">
      {fileRef.projectId}:{fileRef.path}:{fileType}:{String(allowOpenInNewPage)}
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
        '/preview?scope=project&projectId=chat-1&path=%2Fdata%2Fbrief.docx&fileType=docx',
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
        '/preview?scope=project&projectId=chat-1&path=%2Fetc%2Fpasswd',
      ]}>
        <StandalonePreviewPage />
      </MemoryRouter>,
    );

    expect(screen.getByText('Unable to open Preview')).toBeInTheDocument();
    expect(screen.queryByTestId('shared-preview')).not.toBeInTheDocument();
  });
});


function Destination() {
  const location = useLocation();
  return <p data-testid="destination">{location.pathname}{location.search}{location.hash}</p>;
}

it.each(['Back', 'Close Preview'])('returns to the source after direct entry using %s', async (button) => {
  const destination = '/deployments/dep-1?tab=settings#limits';
  render(<MemoryRouter initialEntries={[`/preview?type=workflow&workflowId=wf&version=v1.sv1&returnTo=${encodeURIComponent(destination)}`]}>
    <Routes><Route path="/preview" element={<StandalonePreviewPage />} /><Route path="/deployments/:id" element={<Destination />} /></Routes>
  </MemoryRouter>);
  fireEvent.click(screen.getByRole('button', { name: button }));
  expect(await screen.findByTestId('destination')).toHaveTextContent(destination);
});

it('exits an old direct preview link even without a closeable tab or history', async () => {
  window.history.replaceState(null, '');
  render(<MemoryRouter initialEntries={['/preview?type=workflow&workflowId=wf&version=v1.sv1']}>
    <Routes><Route path="/preview" element={<StandalonePreviewPage />} /><Route path="/" element={<Destination />} /></Routes>
  </MemoryRouter>);
  fireEvent.click(screen.getByRole('button', { name: 'Close Preview' }));
  expect(await screen.findByTestId('destination')).toHaveTextContent('/');
});
