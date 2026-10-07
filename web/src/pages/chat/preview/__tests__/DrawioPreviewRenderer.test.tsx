import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { PreviewDescriptorV1 } from '@/lib/preview/protocol';
import { DrawioPreviewRenderer } from '../DrawioPreviewRenderer';
import { drawioEmbedUrl, exportDrawioXml, openDrawioEditor, renderDrawioXml } from '../drawio-export';

const XML = '<mxGraphModel><root><mxCell id="0"/></root></mxGraphModel>';

function descriptor(): PreviewDescriptorV1 {
  return {
    schemaVersion: 1,
    fileRef: {
      schemaVersion: 1,
      scope: 'project',
      projectId: 'chat-1',
      path: '/data/diagrams/example.drawio',
    },
    name: 'example.drawio',
    sizeBytes: XML.length,
    contentType: 'application/vnd.jgraph.mxfile',
    detectedType: 'drawio',
    revision: 'revision-1',
    renderer: 'drawio',
    loadPolicy: 'range',
    capabilities: { preview: true, edit: false, download: true },
    content: { url: '/api/files/example', rangeSupported: true, truncated: false },
    diagram: { status: 'valid', format: 'drawio', issues: [] },
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('DrawioPreviewRenderer', () => {
  it('hands native XML to the full official draw.io editor without a callback URL', async () => {
    const editor = {
      postMessage: vi.fn(),
      focus: vi.fn(),
    } as unknown as Window;
    const open = vi.spyOn(window, 'open').mockReturnValue(editor);

    const opened = openDrawioEditor(XML, 'example.drawio');
    expect(open).toHaveBeenCalledWith(
      expect.stringMatching(/^https:\/\/app\.diagrams\.net\/\?client=1&/),
      '_blank',
    );
    const destination = new URL(String(open.mock.calls[0][0]));
    expect(destination.searchParams.get('template-filename')).toBe('example.drawio');
    expect(destination.search).not.toContain(XML);

    window.dispatchEvent(new MessageEvent('message', {
      origin: 'https://app.diagrams.net',
      source: editor,
      data: 'ready',
    }));
    await opened;
    expect(editor.postMessage).toHaveBeenCalledWith(XML, 'https://app.diagrams.net');
    expect(editor.focus).toHaveBeenCalledOnce();
  });

  it('renders the official SVG inside the native pan-and-zoom canvas', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(XML, { status: 200, headers: { 'Content-Type': 'application/xml' } }),
    ));
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:preview');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    render(
      <DrawioPreviewRenderer
        descriptor={descriptor()}
        loadAllowed
        onDirtyChange={() => undefined}
      />,
    );
    const frame = await waitFor(() => {
      const element = document.querySelector<HTMLIFrameElement>('iframe[title="draw.io export"]');
      expect(element).not.toBeNull();
      return element!;
    });
    expect(frame.style.width).toBe('1000px');
    expect(frame.style.height).toBe('700px');
    expect(frame.style.left).toBe('-20000px');
    const postMessage = vi.spyOn(frame.contentWindow!, 'postMessage');
    window.dispatchEvent(new MessageEvent('message', {
      origin: drawioEmbedUrl().origin,
      source: frame.contentWindow,
      data: JSON.stringify({ event: 'init' }),
    }));
    await waitFor(() => expect(postMessage).toHaveBeenCalled());
    expect(JSON.parse(String(postMessage.mock.calls[0][0]))).toEqual(expect.objectContaining({
      action: 'export',
      format: 'xmlsvg',
      xml: XML,
    }));
    window.dispatchEvent(new MessageEvent('message', {
      origin: drawioEmbedUrl().origin,
      source: frame.contentWindow,
      data: JSON.stringify({
        event: 'export',
        data: 'data:image/svg+xml;base64,PHN2Zy8+',
      }),
    }));
    expect(await screen.findByRole('img', { name: 'example.drawio' })).toHaveAttribute(
      'data-role',
      'drawio-canvas',
    );
    expect(screen.getByRole('button', { name: 'Continue editing in draw.io' })).toBeVisible();
  });

  it('distinguishes renderer failure from missing content and retries rendering', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(XML)));
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:retry');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const reload = vi.fn();
    render(<DrawioPreviewRenderer descriptor={descriptor()} loadAllowed onDirtyChange={() => undefined} onReload={reload} />);
    const frame = await waitFor(() => {
      const element = document.querySelector<HTMLIFrameElement>('iframe[title="draw.io export"]');
      expect(element).not.toBeNull();return element!;
    });
    window.dispatchEvent(new MessageEvent('message', {origin:drawioEmbedUrl().origin,source:frame.contentWindow,data:JSON.stringify({event:'export',data:'invalid'})}));
    await waitFor(() => expect(document.querySelector('[data-preview-error="render_failed"]')).not.toBeNull());
    expect(document.querySelector('[data-preview-error="content_unavailable"]')).toBeNull();
    vi.mocked(fetch).mockResolvedValue(new Response(XML));
    fireEvent.click(screen.getByRole('button', {name:'Try again'}));
    expect(reload).toHaveBeenCalledOnce();
    const retryFrame = await waitFor(() => {
      const element = document.querySelector<HTMLIFrameElement>('iframe[title="draw.io export"]');
      expect(element).not.toBeNull(); expect(element).not.toBe(frame); return element!;
    });
    window.dispatchEvent(new MessageEvent('message', {origin:drawioEmbedUrl().origin,source:retryFrame.contentWindow,data:JSON.stringify({event:'export',data:'data:image/svg+xml;base64,PHN2Zy8+'})}));
    expect(await screen.findByRole('img', {name:'example.drawio'})).toBeVisible();
  });

  it('sends SVG export through the official diagrams.net embed protocol', async () => {
    const objectUrl = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:export');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    const exported = exportDrawioXml(XML, 'example.drawio', 'svg');
    const frame = document.querySelector<HTMLIFrameElement>('iframe[title="draw.io export"]');
    expect(frame?.src).toBe(drawioEmbedUrl().toString());
    expect(frame?.src).toContain("/api/v1/preview/drawio-assets/index.html");
    const postMessage = vi.spyOn(frame!.contentWindow!, 'postMessage');
    window.dispatchEvent(new MessageEvent('message', {
      origin: drawioEmbedUrl().origin,
      source: frame!.contentWindow,
      data: JSON.stringify({ event: 'init' }),
    }));
    expect(JSON.parse(String(postMessage.mock.calls[0][0]))).toEqual({
      action: 'export',
      format: 'xmlsvg',
      xml: XML,
    });

    window.dispatchEvent(new MessageEvent('message', {
      origin: drawioEmbedUrl().origin,
      source: frame!.contentWindow,
      data: JSON.stringify({
        event: 'export',
        data: 'data:image/svg+xml;base64,PHN2Zy8+',
      }),
    }));
    await exported;
    expect(objectUrl).toHaveBeenCalledWith(expect.objectContaining({
      type: 'image/svg+xml',
    }));
  });

  it('creates a real PDF from the official PNG when no draw.io export server is configured', async () => {
    const exported = renderDrawioXml(XML, 'pdf');
    const frame = document.querySelector<HTMLIFrameElement>('iframe[title="draw.io export"]')!;
    const postMessage = vi.spyOn(frame.contentWindow!, 'postMessage');
    window.dispatchEvent(new MessageEvent('message', {
      origin: drawioEmbedUrl().origin,
      source: frame.contentWindow,
      data: JSON.stringify({ event: 'init' }),
    }));
    expect(JSON.parse(String(postMessage.mock.calls[0][0]))).toEqual(expect.objectContaining({
      action: 'export',
      format: 'xmlpng',
    }));
    window.dispatchEvent(new MessageEvent('message', {
      origin: drawioEmbedUrl().origin,
      source: frame.contentWindow,
      data: JSON.stringify({
        event: 'export',
        data: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
      }),
    }));
    const blob = await exported;
    expect(blob.type).toBe('application/pdf');
    expect(new TextDecoder().decode((await blob.arrayBuffer()).slice(0, 4))).toBe('%PDF');
  });

  it('creates a real JPEG from the official PNG render', async () => {
    const close = vi.fn();
    vi.stubGlobal('createImageBitmap', vi.fn().mockResolvedValue({ width: 1, height: 1, close }));
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      fillStyle: '',
      fillRect: vi.fn(),
      imageSmoothingEnabled: false,
      imageSmoothingQuality: 'low',
      drawImage: vi.fn(),
    } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation((callback) => {
      callback(new Blob(['jpeg'], { type: 'image/jpeg' }));
    });

    const exported = renderDrawioXml(XML, 'jpg');
    const frame = document.querySelector<HTMLIFrameElement>('iframe[title="draw.io export"]')!;
    window.dispatchEvent(new MessageEvent('message', {
      origin: drawioEmbedUrl().origin,
      source: frame.contentWindow,
      data: JSON.stringify({ event: 'init' }),
    }));
    window.dispatchEvent(new MessageEvent('message', {
      origin: drawioEmbedUrl().origin,
      source: frame.contentWindow,
      data: JSON.stringify({
        event: 'export',
        data: 'data:image/png;base64,iVBORw0KGgo=',
      }),
    }));
    const blob = await exported;
    expect(blob.type).toBe('image/jpeg');
    expect(close).toHaveBeenCalledOnce();
  });
});
