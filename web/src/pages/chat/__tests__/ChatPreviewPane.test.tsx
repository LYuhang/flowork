import { forwardRef, useImperativeHandle, type ComponentProps } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { ChatPreviewItem } from '@/lib/chat/preview-state';
import { ChatPreviewPane } from '../ChatPreviewPane';

const fileViewer = vi.hoisted(() => ({
  requestLeave: vi.fn<(onLeave: () => void) => void>(),
}));

vi.mock('../preview/ChatFilePreview', () => ({
  ChatFilePreview: forwardRef(function MockChatFilePreview(_props, ref) {
    useImperativeHandle(ref, () => ({ requestLeave: fileViewer.requestLeave }));
    return <div>Active file</div>;
  }),
}));

vi.mock('../ChatWorkflowViewer', () => ({
  ChatWorkflowViewer: () => <div>Workflow preview</div>,
}));
vi.mock('../preview/WorkflowPreviewRenderer', () => ({
  WorkflowPreviewRenderer: ({ workflowId, version }: { workflowId: string; version: string }) => <div>Snapshot {workflowId} {version}</div>,
}));

const items: ChatPreviewItem[] = [
  {
    id: 'file-1',
    title: 'notes.md',
    resource: {
      schemaVersion: 1,
      kind: 'file',
      fileRef: { schemaVersion: 1, scope: 'project', projectId: 'chat-1', path: '/data/notes.md' },
    },
  },
  {
    id: 'workflow-1',
    title: 'Review workflow',
    resource: { schemaVersion: 1, kind: 'workflow', workflowId: 'wf-1' },
  },
];

function renderPane(overrides: Partial<ComponentProps<typeof ChatPreviewPane>> = {}) {
  const props: ComponentProps<typeof ChatPreviewPane> = {
    scopeId: 'scope-1',
    open: true,
    items,
    resources: items,
    activeId: 'file-1',
    onToggleOpen: vi.fn(),
    onSelect: vi.fn(),
    onOpenResource: vi.fn(),
    onCloseItem: vi.fn(),
    ...overrides,
  };
  render(<ChatPreviewPane {...props} />);
  return props;
}

describe('ChatPreviewPane dirty-file leave protocol', () => {
  it('renders a pinned workflow artifact in the side pane', async () => {
    const item: ChatPreviewItem = {
      id: 'interactive:wf-preview', title: 'Workflow snapshot',
      resource: { schemaVersion: 1, kind: 'interactive', artifactId: 'wf-preview' },
      artifact: { kind: 'interactive_artifact', artifact_id: 'wf-preview', component_type: 'workflow_preview',
        completion_mode: 'render_only', props: { workflow_id: 'wf', version: 'v1.sv3' } },
    };
    renderPane({ items: [item], resources: [item], activeId: item.id });
    expect(await screen.findByText('Snapshot wf v1.sv3')).toBeInTheDocument();
    expect(document.querySelector('[data-role="interactive-artifact-preview-surface"]')).toHaveClass('h-full');
  });
  it('renders a URL artifact in the side pane with its original address', async () => {
    const urlItem: ChatPreviewItem = {
      id: 'interactive:url-1', title: 'Reference website',
      resource: { schemaVersion: 1, kind: 'interactive', artifactId: 'url-1' },
      artifact: {
        kind: 'interactive_artifact', artifact_id: 'url-1',
        title: 'Reference website', component_type: 'url_preview',
        completion_mode: 'render_only', props: { url: 'https://example.com/docs' },
      },
    };
    renderPane({ items: [urlItem], resources: [urlItem], activeId: urlItem.id });
    const frame = await waitFor(() => {
      const element = document.querySelector('iframe[title="Reference website"]');
      expect(element).toHaveAttribute('src', 'https://example.com/docs');
      return element;
    });
    expect(frame).toHaveAttribute('sandbox', expect.stringContaining('allow-scripts'));
    expect(document.querySelector('[data-role="interactive-artifact-preview-surface"]')).toHaveClass('h-full');
  });

  it('defers tab selection until the active file viewer allows leaving', async () => {
    fileViewer.requestLeave.mockReset();
    const props = renderPane();
    await screen.findByText('Active file');

    fireEvent.click(screen.getByRole('button', { name: 'Review workflow' }));
    expect(fileViewer.requestLeave).toHaveBeenCalledOnce();
    expect(props.onSelect).not.toHaveBeenCalled();

    fileViewer.requestLeave.mock.calls[0]?.[0]();
    expect(props.onSelect).toHaveBeenCalledWith('workflow-1');
  });

  it('uses the same guard before closing the whole preview pane', async () => {
    fileViewer.requestLeave.mockReset();
    const props = renderPane();
    await screen.findByText('Active file');

    fireEvent.click(screen.getByRole('button', { name: /^close preview$/i }));
    expect(fileViewer.requestLeave).toHaveBeenCalledOnce();
    expect(props.onToggleOpen).not.toHaveBeenCalled();
  });

  it('constrains long file content to the Preview viewport', async () => {
    renderPane();
    await screen.findByText('Active file');

    expect(document.querySelector('[data-role="chat-preview-content"]')).toHaveClass(
      'min-h-0',
      'overflow-hidden',
    );
  });
});
