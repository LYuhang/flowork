import { getApiBase } from '@/lib/base-path';
const DEFAULT_DRAWIO_EDITOR_URL = 'https://app.diagrams.net/';

export type DrawioExportFormat = 'drawio' | 'svg' | 'png' | 'pdf' | 'jpg';
export type DrawioRenderedFormat = Exclude<DrawioExportFormat, 'drawio'>;
type DrawioNativeRenderedFormat = Extract<DrawioRenderedFormat, 'svg' | 'png'>;

export function drawioEmbedUrl(): URL {
  const url = new URL(`${getApiBase()}/api/v1/preview/drawio-assets/index.html`, window.location.origin);
  url.search = new URLSearchParams({
    embed: '1',
    offline: '1',
    pwa: '0',
    local: '1',
    analytics: '0',
    ui: 'min',
    spin: '1',
    proto: 'json',
    libraries: '0',
    grid: '0',
    pv: '0',
    saveAndExit: '0',
    noSaveBtn: '1',
    noExitBtn: '1',
    tooltips: '0',
  }).toString();
  return url;
}

/**
 * Open the full official draw.io editor and hand it the current native XML.
 *
 * `client=1` is the diagrams.net client-mode protocol: the editor opens as a
 * normal application, sends `ready` to its opener, and accepts XML through
 * `postMessage`. This deliberately has no save callback. From this point the
 * user owns the independent draw.io copy and can save/export it there, exactly
 * like a download followed by an import but without the intermediate steps.
 */
export function openDrawioEditor(xml: string, name: string): Promise<void> {
  const configured = import.meta.env.VITE_DRAWIO_EDITOR_URL as string | undefined;
  const editorUrl = new URL(configured?.trim() || DEFAULT_DRAWIO_EDITOR_URL);
  editorUrl.search = new URLSearchParams({
    client: '1',
    spin: '1',
    libraries: '1',
    'template-filename': name || 'diagram.drawio',
  }).toString();

  // Client mode needs a live opener until the one-way XML hand-off completes.
  // The destination origin is fixed/configured by the deployment and never
  // comes from diagram content or user input.
  const editor = window.open(editorUrl.toString(), '_blank');
  if (!editor) return Promise.reject(new Error('The browser blocked the draw.io window'));

  return new Promise<void>((resolve, reject) => {
    let settled = false;
    const cleanup = () => {
      window.removeEventListener('message', receive);
      window.clearTimeout(timeout);
    };
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) reject(error);
      else resolve();
    };
    const receive = (event: MessageEvent) => {
      if (event.origin !== editorUrl.origin || event.source !== editor) return;
      const ready = event.data === 'ready'
        || (typeof event.data === 'object'
          && event.data !== null
          && (event.data as { event?: unknown }).event === 'ready');
      if (!ready) return;
      try {
        editor.postMessage(xml, editorUrl.origin);
        editor.focus();
        finish();
      } catch (error) {
        finish(error instanceof Error ? error : new Error(String(error)));
      }
    };
    const timeout = window.setTimeout(
      () => finish(new Error('draw.io did not become ready in time')),
      30_000,
    );
    window.addEventListener('message', receive);
  });
}

function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.style.display = 'none';
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  // Edge may not start reading a Blob URL until after the current task. Revoking
  // immediately makes genuine downloads intermittently fail as "canceled".
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

function dataUriToBlob(dataUri: string, fallbackMime: string): Blob {
  const match = /^data:([^;,]*)(;base64)?,([\s\S]*)$/.exec(dataUri);
  if (!match) throw new Error('draw.io returned an invalid export payload');
  const mime = match[1] || fallbackMime;
  if (match[2]) {
    const binary = window.atob(match[3]);
    const bytes = Uint8Array.from(binary, (value) => value.charCodeAt(0));
    return new Blob([bytes], { type: mime });
  }
  return new Blob([decodeURIComponent(match[3])], { type: mime });
}

function exportedFilename(name: string, format: DrawioExportFormat): string {
  const base = name.replace(/\.drawio$/i, '') || 'diagram';
  if (format === 'drawio') return `${base}.drawio`;
  if (format === 'svg' || format === 'png') {
    return `${base}.drawio.${format}`;
  }
  return `${base}.${format}`;
}

/** Render exact native XML through the official diagrams.net embed protocol. */
async function renderNativeDrawioXml(
  xml: string,
  format: DrawioNativeRenderedFormat,
): Promise<Blob> {
  const embedUrl = drawioEmbedUrl();
  const frame = document.createElement('iframe');
  frame.title = 'draw.io export';
  frame.src = embedUrl.toString();
  // diagrams.net needs a real viewport to initialize its renderer reliably. Keep the
  // official renderer off-screen; the exported SVG is displayed on our native canvas.
  frame.style.cssText = [
    'position:fixed',
    'width:1000px',
    'height:700px',
    'left:-20000px',
    'top:0',
    'border:0',
    'pointer-events:none',
  ].join(';');
  frame.tabIndex = -1;
  frame.setAttribute('aria-hidden', 'true');
  frame.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-downloads');

  const officialFormat = {
    svg: 'xmlsvg',
    png: 'xmlpng',
  }[format];
  const mime = {
    svg: 'image/svg+xml',
    png: 'image/png',
  }[format];

  return await new Promise<Blob>((resolve, reject) => {
    let settled = false;
    const cleanup = () => {
      window.removeEventListener('message', receive);
      frame.remove();
      window.clearTimeout(timeout);
    };
    const finish = (blob?: Blob, error?: Error) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) reject(error);
      else if (blob) resolve(blob);
      else reject(new Error('draw.io returned an empty export payload'));
    };
    const receive = (event: MessageEvent) => {
      if (event.origin !== embedUrl.origin || event.source !== frame.contentWindow) return;
      let message: { event?: string; data?: string };
      try {
        message = typeof event.data === 'string' ? JSON.parse(event.data) : event.data;
      } catch {
        return;
      }
      if (message.event === 'init') {
        frame.contentWindow?.postMessage(JSON.stringify({
          action: 'export',
          format: officialFormat,
          xml,
        }), embedUrl.origin);
      } else if (message.event === 'export') {
        if (typeof message.data !== 'string' || !message.data.startsWith('data:')) {
          finish(undefined, new Error('draw.io returned an invalid export payload'));
          return;
        }
        try {
          finish(dataUriToBlob(message.data, mime));
        } catch (error) {
          finish(undefined, error instanceof Error ? error : new Error(String(error)));
        }
      }
    };
    const timeout = window.setTimeout(
      () => finish(undefined, new Error('draw.io export timed out')),
      30_000,
    );
    window.addEventListener('message', receive);
    document.body.append(frame);
  });
}

async function pngToJpeg(png: Blob): Promise<Blob> {
  const bitmap = await createImageBitmap(png);
  try {
    const canvas = document.createElement('canvas');
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const context = canvas.getContext('2d');
    if (!context) throw new Error('The browser cannot create a JPEG export canvas');
    context.fillStyle = '#ffffff';
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = 'high';
    context.drawImage(bitmap, 0, 0);
    return await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob((blob) => {
        if (blob) resolve(blob);
        else reject(new Error('The browser could not encode the JPEG export'));
      }, 'image/jpeg', 0.92);
    });
  } finally {
    bitmap.close();
  }
}

async function pngToPdf(png: Blob): Promise<Blob> {
  const { PDFDocument } = await import('pdf-lib');
  const document = await PDFDocument.create();
  const image = await document.embedPng(await png.arrayBuffer());
  const landscape = image.width >= image.height;
  const pageWidth = landscape ? 841.89 : 595.28;
  const pageHeight = landscape ? 595.28 : 841.89;
  const margin = 36;
  const scale = Math.min(
    (pageWidth - margin * 2) / image.width,
    (pageHeight - margin * 2) / image.height,
  );
  const width = image.width * scale;
  const height = image.height * scale;
  const page = document.addPage([pageWidth, pageHeight]);
  page.drawImage(image, {
    x: (pageWidth - width) / 2,
    y: (pageHeight - height) / 2,
    width,
    height,
  });
  const saved = await document.save();
  const bytes = new Uint8Array(saved.byteLength);
  bytes.set(saved);
  return new Blob([bytes.buffer], { type: 'application/pdf' });
}

/**
 * Render a downloadable representation from the official diagrams.net output.
 * The hosted embed endpoint renders SVG and editable PNG locally. Its PDF path
 * requires a separately configured export server, so PDF/JPG are encoded in
 * the browser from the exact official PNG rather than accepting an SVG payload
 * with the wrong extension.
 */
export async function renderDrawioXml(
  xml: string,
  format: DrawioRenderedFormat,
): Promise<Blob> {
  if (format === 'jpg') return pngToJpeg(await renderNativeDrawioXml(xml, 'png'));
  if (format === 'pdf') return pngToPdf(await renderNativeDrawioXml(xml, 'png'));
  return renderNativeDrawioXml(xml, format);
}

/** Export exact native XML through the official diagrams.net embed protocol. */
export async function exportDrawioXml(
  xml: string,
  name: string,
  format: DrawioExportFormat,
): Promise<void> {
  if (format === 'drawio') {
    downloadBlob(
      new Blob([xml], { type: 'application/vnd.jgraph.mxfile' }),
      exportedFilename(name, format),
    );
    return;
  }
  downloadBlob(await renderDrawioXml(xml, format), exportedFilename(name, format));
}
