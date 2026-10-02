import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { TooltipProvider } from '@/components/ui/tooltip';
import type { PreviewDescriptorV1 } from '@/lib/preview/protocol';
import { MarkdownPreviewRenderer } from '../TextPreviewRenderers';

const mutateAsync = vi.hoisted(() => vi.fn());
const createPreviewResourceSession = vi.hoisted(() => vi.fn());

vi.mock('@/lib/api/previews', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/api/previews')>(),
  createPreviewResourceSession,
}));

vi.mock('@/lib/api/queries/previews', () => ({
  useWritePreviewFile: () => ({
    mutateAsync,
    isPending: false,
    error: null,
  }),
}));

const descriptor = {
  schemaVersion: 1,
  fileRef: {
    schemaVersion: 1,
    scope: 'project',
    projectId: 'chat-1',
    path: '/data/notes.md',
  },
  name: 'notes.md',
  sizeBytes: 24,
  contentType: 'text/markdown',
  detectedType: 'markdown',
  revision: 'sha256:notes',
  renderer: 'markdown',
  loadPolicy: 'inline',
  capabilities: { preview: true, edit: true, download: true },
  content: {
    inlineText: '# Original\n',
    truncated: false,
    rangeSupported: false,
  },
} satisfies PreviewDescriptorV1;

beforeEach(() => {
  createPreviewResourceSession.mockResolvedValue({
    schemaVersion: 1,
    resourceMounts: [{
      pathPrefix: '/',
      rootUrl: 'https://api.test/resources/capability/',
    }],
    baseUrl: 'https://api.test/resources/capability/data/',
    expiresIn: 3600,
  });
});

describe('TextDocumentRenderer editing lifecycle', () => {
  it('renders document math while preserving code and rejecting trusted TeX commands', async () => {
    const source = String.raw`# Formula

Inline $E = mc^2$ and \(x + 1\).

\[
\frac{1}{2}
\]

Literal code: `;
    const markdown = source + '`$keep_literal$`\n\n'
      + String.raw`$\href{javascript:alert(1)}{unsafe}$`;
    const { container } = render(<MarkdownPreviewRenderer
      descriptor={{ ...descriptor, content: { ...descriptor.content, inlineText: markdown } }}
      loadAllowed
      onDirtyChange={() => undefined}
    />);
    await waitFor(() => expect(container.querySelectorAll('.katex')).toHaveLength(4), { timeout: 15000 });
    expect(container.querySelector('.katex-display')).toBeInTheDocument();
    expect(screen.getByText('$keep_literal$')).toBeInTheDocument();
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
  }, 20000);

  it('exits a clean editing session directly', async () => {
    render(
      <MarkdownPreviewRenderer
        descriptor={descriptor}
        loadAllowed
        onDirtyChange={() => undefined}
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    expect(screen.getByRole('button', { name: 'Exit editing' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Exit editing' }));

    expect(screen.queryByRole('button', { name: 'Exit editing' })).not.toBeInTheDocument();
    expect(screen.queryByText('Discard unsaved changes?')).not.toBeInTheDocument();
  });

  it('requires confirmation before discarding dirty edits', async () => {
    const dirtyChanges: boolean[] = [];
    render(
      <MarkdownPreviewRenderer
        descriptor={descriptor}
        loadAllowed
        onDirtyChange={(dirty) => dirtyChanges.push(dirty)}
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'notes.md source' }), {
      target: { value: '# Changed\n' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Exit editing' }));

    expect(screen.getByText('Discard unsaved changes?')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }));
    expect(screen.getByRole('button', { name: 'Exit editing' })).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: 'Exit editing' }));
    fireEvent.click(screen.getByRole('button', { name: 'Discard and exit' }));

    expect(screen.queryByRole('button', { name: 'Exit editing' })).not.toBeInTheDocument();
    expect(screen.getByText('# Original')).toBeVisible();
    expect(dirtyChanges).toContain(true);
    expect(dirtyChanges.at(-1)).toBe(false);
  });

  it('keeps dirty edits visible when a newer descriptor revision arrives', async () => {
    const view = render(
      <MarkdownPreviewRenderer
        descriptor={descriptor}
        loadAllowed
        onDirtyChange={() => undefined}
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'notes.md source' }), {
      target: { value: '# Unsaved local draft\n' },
    });
    view.rerender(
      <MarkdownPreviewRenderer
        descriptor={{
          ...descriptor,
          revision: 'sha256:newer',
          content: { ...descriptor.content, inlineText: '# New remote version\n' },
        }}
        loadAllowed
        onDirtyChange={() => undefined}
      />,
    );

    expect(screen.getByRole('textbox', { name: 'notes.md source' })).toHaveValue(
      '# Unsaved local draft\n',
    );
    expect(screen.getByRole('alert')).toHaveTextContent(
      'This file changed after editing began.',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Reload' }));
    await waitFor(() => expect(
      screen.getByText('# New remote version'),
    ).toBeVisible());
  });

  it('keeps Markdown heading links inside Preview instead of changing the app URL', async () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    window.history.replaceState(null, '', '/chat');
    render(
      <MarkdownPreviewRenderer
        descriptor={{
          ...descriptor,
          content: {
            ...descriptor.content,
            inlineText: '[Release review](#release-review)\n\n## Release review\n',
          },
        }}
        loadAllowed
        onDirtyChange={() => undefined}
      />,
    );

    const heading = await screen.findByRole('heading', { name: 'Release review' });
    expect(heading).toHaveAttribute('id', 'release-review');
    fireEvent.click(screen.getByRole('link', { name: 'Release review' }));

    expect(window.location.pathname).toBe('/chat');
    expect(window.location.hash).toBe('');
    expect(scrollIntoView).toHaveBeenCalledOnce();
  });

  it('loads a same-directory Markdown image through its resource capability', async () => {
    render(
      <MarkdownPreviewRenderer
        descriptor={{
          ...descriptor,
          content: {
            ...descriptor.content,
            inlineText: '![Architecture](handbook-architecture.svg)',
          },
        }}
        loadAllowed
        onDirtyChange={() => undefined}
      />,
    );

    await screen.findByRole('img', { name: 'Architecture' });
    await waitFor(() => expect(
      screen.getByRole('img', { name: 'Architecture' }),
    ).toHaveAttribute(
      'src',
      'https://api.test/resources/capability/data/handbook-architecture.svg',
    ));
    expect(createPreviewResourceSession).toHaveBeenCalledWith(
      descriptor.fileRef,
      expect.any(AbortSignal),
    );
  });

  it('renders a complete GFM document with document typography and professional data surfaces', async () => {
    render(
      <TooltipProvider>
        <MarkdownPreviewRenderer
          descriptor={{
            ...descriptor,
            content: {
              ...descriptor.content,
              inlineText: [
                '# Release brief',
                '',
                '> Review the rollout before publishing.',
                '',
                '- [x] Automated checks',
                '- [ ] Production approval',
                '',
                '| Area | Result |',
                '| --- | --- |',
                '| API | Ready |',
                '',
                '```python',
                'print("ready")',
                '```',
              ].join('\n'),
            },
          }}
          loadAllowed
          onDirtyChange={() => undefined}
        />
      </TooltipProvider>,
    );

    const documentSurface = await screen.findByRole('article');
    expect(documentSurface).toHaveAttribute('data-role', 'markdown-document');
    expect(documentSurface).toHaveClass('markdown-document');
    expect(screen.getByRole('heading', { name: 'Release brief' })).toBeInTheDocument();
    expect(screen.getAllByRole('checkbox')).toHaveLength(2);
    expect(document.querySelector('.markdown-document-table-wrap table')).not.toBeNull();
    expect(document.querySelector('[data-language="python"]')).not.toBeNull();
    expect(screen.getByText('print("ready")')).toBeInTheDocument();
  });
});
