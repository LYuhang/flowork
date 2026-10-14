import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { MouseEvent, ReactNode } from 'react';
import { beforeEach, expect, it, vi } from 'vitest';
import '@/lib/i18n';
import type { ChatAttachment } from '@/components/agent-sidebar/chat-attachments';
import type { PreviewDescriptorV1 } from '@/lib/preview/protocol';
import { MarkdownPreviewRenderer, TextPreviewRenderer } from '../TextPreviewRenderers';
import { StructuredTablePreview } from '../StructuredTablePreview';
import { MediaPreviewRenderer } from '../MediaPreviewRenderer';

const capture = vi.hoisted(() => vi.fn());
vi.mock('../PreviewContextMenu', () => ({
  PreviewContextMenu: ({ children, build }: { children: ReactNode; build: (event: MouseEvent<HTMLDivElement>) => ChatAttachment }) =>
    <div data-testid="quote-surface" onContextMenu={event => capture(build(event))}>{children}</div>,
}));
vi.mock('@/lib/api/queries/previews', () => ({ useWritePreviewFile: () => ({ mutateAsync: vi.fn(), isPending: false }) }));
vi.mock('@/lib/api/previews', async original => ({
  ...await original<typeof import('@/lib/api/previews')>(),
  createPreviewResourceSession: vi.fn().mockResolvedValue({ baseUrl: '/resources/', resourceMounts: [] }),
}));
vi.mock('ag-grid-react', () => ({ AgGridReact: ({ rowData, getRowId }: {
  rowData: Record<string, string>[]; getRowId: (params: { data: Record<string, string> }) => string;
}) => <div>{[...rowData].reverse().map(data => <div key={getRowId({ data })} row-id={getRowId({ data })}>
  <span col-id="c0">{data.c0}</span>
</div>)}<span col-id="c1">Column header</span></div> }));
const descriptor: PreviewDescriptorV1 = {
  schemaVersion: 1, fileRef: { schemaVersion: 1, scope: 'project', projectId: 'p', path: '/data/sample' },
  name: 'sample', revision: 'v1', sizeBytes: 100, contentType: 'text/plain', detectedType: 'markdown',
  renderer: 'markdown', loadPolicy: 'inline', capabilities: { preview: true, edit: true, download: true },
  content: { inlineText: 'Alpha paragraph\n\nBeta paragraph', truncated: false, rangeSupported: false },
};
beforeEach(() => { capture.mockClear(); window.getSelection()?.removeAllRanges(); });
it('quotes the clicked Markdown block instead of a previous selection in a different block', async () => {
  render(<MarkdownPreviewRenderer descriptor={descriptor} loadAllowed onDirtyChange={() => undefined} />);
  const alpha = await screen.findByText('Alpha paragraph');
  const beta = screen.getByText('Beta paragraph');
  const range = document.createRange(); range.selectNodeContents(alpha);
  range.getClientRects = () => [{ left: 0, top: 0, right: 50, bottom: 20 }] as unknown as DOMRectList;
  window.getSelection()!.addRange(range);
  fireEvent.contextMenu(beta, { clientX: 20, clientY: 100 });
  expect(capture.mock.lastCall![0].snapshot.text).toBe('Beta paragraph');
  expect(capture.mock.lastCall![0].source.revision).toBe('v1');
});
it('keeps original CSV coordinates when displayed rows are reordered', async () => {
  render(<StructuredTablePreview descriptor={{ ...descriptor, detectedType: 'csv', content: { ...descriptor.content!, inlineText: 'name,value\nFirst,1\n\nSecond,2' } }} loadAllowed onDirtyChange={() => undefined} />);
  fireEvent.contextMenu(await screen.findByText('Second'));
  expect(capture.mock.lastCall![0].selector).toMatchObject({ kind: 'cells', first_row: 4, last_row: 4, first_column: 1 });
  fireEvent.contextMenu(screen.getByText('Column header'));
  expect(capture.mock.lastCall![0].selector).toMatchObject({ first_row: 1, last_row: 4, first_column: 2 });
});
it('quotes JSONL source records with physical line numbers, including blank lines', async () => {
  const text = '\n{"name":"First"}\n\n{"name":"Second"}';
  render(<StructuredTablePreview descriptor={{ ...descriptor, detectedType: 'jsonl', content: { ...descriptor.content!, inlineText: text } }} loadAllowed onDirtyChange={() => undefined} />);
  fireEvent.contextMenu(await screen.findByText('Second'));
  expect(capture.mock.lastCall![0].selector).toMatchObject({ kind: 'text', start_line: 4, end_line: 4 });
  expect(capture.mock.lastCall![0].snapshot.text).toBe('{"name":"Second"}');
});
it('freezes media time on the media surface and quotes the whole file on surrounding space', async () => {
  const { container } = render(<MediaPreviewRenderer descriptor={{ ...descriptor, renderer: 'video', content: { url: '/clip.webm', truncated: false, rangeSupported: true } }} loadAllowed onDirtyChange={() => undefined} />);
  const video = container.querySelector('video')!;
  Object.defineProperty(video, 'readyState', { value: 1 });
  video.currentTime = 12.75;
  fireEvent.contextMenu(video);
  video.currentTime = 20;
  expect(capture.mock.lastCall![0].selector).toEqual({ kind: 'time', start_seconds: 12.75 });
  fireEvent.contextMenu(screen.getByTestId('quote-surface'));
  await waitFor(() => expect(capture.mock.lastCall![0].type).toBe('file'));
});

it('quotes the file on raw-text background instead of copying the whole document as an excerpt', async () => {
  const { container } = render(<TextPreviewRenderer descriptor={{ ...descriptor, name: 'plain.txt', renderer: 'text', detectedType: 'text' }} loadAllowed onDirtyChange={() => undefined} />);
  await waitFor(() => expect(container.querySelector('[data-role="preview-source-text"]')).toBeInTheDocument());
  fireEvent.contextMenu(container.querySelector('[data-role="preview-source-text"]')!);
  expect(capture.mock.lastCall![0].type).toBe('file');
});
