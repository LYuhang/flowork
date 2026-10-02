import { PreviewToolbar } from './PreviewToolbar';
import { PreviewContextMenu } from './PreviewContextMenu';
import { filePageReference } from '@/lib/preview/file-reference';
import { useEffect, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, Maximize2, Minus, Plus } from 'lucide-react';
import type { PDFDocumentProxy, PDFPageProxy } from 'pdfjs-dist';
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import { getBasePath } from '@/lib/base-path';

import { Button } from '@/components/ui/button';
import { useTranslation } from 'react-i18next';
import { fetchPreviewRendition } from '@/lib/api/previews';
import type { PreviewErrorInfo } from '@/lib/preview/protocol';
import { PreviewErrorState } from './PreviewErrorState';
import { initialPdfPreviewZoom, MIN_PDF_ZOOM } from './pdf-preview-layout';
import type { PreviewRendererProps } from './renderer-types';

// The worker asset content hash predates the nginx `.mjs` MIME fix. Keep a
// URL-level revision so browsers that cached the old octet-stream response do
// not reuse its invalid Content-Type after an application upgrade.
const PDF_WORKER_CACHE_REVISION = 'module-mime-v1';
const PREVIEW_WIDTH_SAMPLE_PAGES = 64;

export function PdfPreviewRenderer({ descriptor, initialPage }: PreviewRendererProps) {
  const rendition = descriptor.rendition?.format === 'pdf'
    ? descriptor.rendition
    : null;
  const url = rendition?.url ?? descriptor.content?.url;
  if (!url) {
    return (
      <PreviewErrorState
        descriptor={descriptor}
        error={{ code: 'content_unavailable', params: {} }}
      />
    );
  }
  return (
    <PdfPreviewContent
      key={`${descriptor.revision}:${url}:${initialPage ?? 1}`}
      url={url}
      authenticated={Boolean(rendition)}
      initialPage={initialPage}
      descriptor={descriptor}
    />
  );
}

function PdfPreviewContent({
  url,
  authenticated,
  descriptor,
  initialPage = 1,
}: {
  url: string;
  authenticated: boolean;
  initialPage?: number;
  descriptor: PreviewRendererProps['descriptor'];
}) {
  const { t } = useTranslation();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const scrollAreaRef = useRef<HTMLDivElement>(null);
  const documentRef = useRef<PDFDocumentProxy | null>(null);
  const pageRef = useRef<PDFPageProxy | null>(null);
  const [pageNumber, setPageNumber] = useState(initialPage);
  const [pageCount, setPageCount] = useState(0);
  const [zoom, setZoom] = useState(1.25);
  const [naturalWidth, setNaturalWidth] = useState(0);
  const [fitToWidth, setFitToWidth] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<PreviewErrorInfo | null>(null);

  useEffect(() => {
    let disposed = false;
    const controller = new AbortController();
    let task: ReturnType<typeof import('pdfjs-dist')['getDocument']> | null = null;
    void import('pdfjs-dist').then(async (pdfjs) => {
      pdfjs.GlobalWorkerOptions.workerSrc = `${pdfWorkerUrl}?v=${PDF_WORKER_CACHE_REVISION}`;
      const source = authenticated
        ? {
            data: new Uint8Array(
              await fetchPreviewRendition(url, controller.signal),
            ),
          }
        : {
            url,
            rangeChunkSize: 64 * 1024,
            disableAutoFetch: true,
          };
      if (disposed) return;
      const assets = new URL(`${getBasePath()}/assets/pdfjs/${pdfjs.version}/`, window.location.origin).href;
      task = pdfjs.getDocument({
        ...source,
        cMapUrl: `${assets}cmaps/`,
        cMapPacked: true,
        standardFontDataUrl: `${assets}standard_fonts/`,
        wasmUrl: `${assets}wasm/`,
        iccUrl: `${assets}iccs/`,
        // Use PDF.js's bundled JS fallback decoders under the application's CSP.
        useWasm: false,
      });
      const document = await task.promise;
      if (disposed) return;
      documentRef.current = document;
      if (!Number.isSafeInteger(initialPage) || initialPage < 1 || initialPage > document.numPages) {
        setError({ code: 'content_unavailable', params: {} });
        return;
      }
      setPageCount(document.numPages);
      let maximumNaturalWidth = 0;
      const sampledPages = Math.min(document.numPages, PREVIEW_WIDTH_SAMPLE_PAGES);
      for (let index = 1; index <= sampledPages; index += 1) {
        const page = await document.getPage(index);
        if (disposed) return;
        maximumNaturalWidth = Math.max(
          maximumNaturalWidth,
          page.getViewport({ scale: 1 }).width,
        );
        page.cleanup();
      }
      setNaturalWidth(maximumNaturalWidth);
    }).catch(() => {
      if (!disposed) setError({ code: 'render_failed', params: {} });
    }).finally(() => {
      if (!disposed) setLoading(false);
    });
    return () => {
      disposed = true;
      controller.abort();
      pageRef.current?.cleanup();
      pageRef.current = null;
      documentRef.current = null;
      if (task) void task.destroy();
    };
  }, [authenticated, url, initialPage]);

  useEffect(() => {
    const container = scrollAreaRef.current;
    if (!container || !fitToWidth || naturalWidth <= 0) return;
    const updateZoom = () => {
      setZoom(initialPdfPreviewZoom(naturalWidth, container.clientWidth));
    };
    updateZoom();
    const observer = new ResizeObserver(updateZoom);
    observer.observe(container);
    return () => observer.disconnect();
  }, [fitToWidth, naturalWidth]);

  useEffect(() => {
    const document = documentRef.current;
    const canvas = canvasRef.current;
    if (!document || !canvas || pageCount === 0) return;
    let disposed = false;
    let renderTask: { cancel: () => void; promise: Promise<unknown> } | null = null;
    void document.getPage(pageNumber).then((page) => {
      if (disposed) return;
      pageRef.current?.cleanup();
      pageRef.current = page;
      const viewport = page.getViewport({ scale: zoom * window.devicePixelRatio });
      const context = canvas.getContext('2d');
      if (!context) throw new Error('Canvas is unavailable.');
      canvas.width = Math.ceil(viewport.width);
      canvas.height = Math.ceil(viewport.height);
      canvas.style.width = `${Math.ceil(viewport.width / window.devicePixelRatio)}px`;
      canvas.style.height = `${Math.ceil(viewport.height / window.devicePixelRatio)}px`;
      renderTask = page.render({ canvas, canvasContext: context, viewport });
      return renderTask.promise;
    }).catch((reason) => {
      if (!disposed && reason?.name !== 'RenderingCancelledException') {
        setError({ code: 'render_failed', params: {} });
      }
    });
    return () => {
      disposed = true;
      renderTask?.cancel();
    };
  }, [pageCount, pageNumber, zoom]);

  if (error) return <PreviewErrorState descriptor={descriptor} error={error} />;
  return (
    <div className="flex h-full min-h-0 flex-col">
      <PreviewToolbar>

        <Button
          variant="ghost"
          size="icon-sm"
          disabled={pageNumber <= 1}
          aria-label={t('preview.pdf.previousPage', 'Previous page')}
          onClick={() => setPageNumber((value) => Math.max(1, value - 1))}
        >
          <ChevronLeft className="h-4 w-4" />
        </Button>
        <span className="min-w-24 text-center text-xs text-muted-foreground">
          {pageCount ? `${pageNumber} / ${pageCount}` : 'Loading…'}
        </span>
        <Button
          variant="ghost"
          size="icon-sm"
          disabled={!pageCount || pageNumber >= pageCount}
          aria-label={t('preview.pdf.nextPage', 'Next page')}
          onClick={() => setPageNumber((value) => Math.min(pageCount, value + 1))}
        >
          <ChevronRight className="h-4 w-4" />
        </Button>
        <span className="mx-1 h-4 w-px bg-edge-subtle" />
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={t('preview.action.zoomOut', 'Zoom out')}
          onClick={() => {
            setFitToWidth(false);
            setZoom((value) => Math.max(MIN_PDF_ZOOM, value - 0.25));
          }}
        >
          <Minus className="h-4 w-4" />
        </Button>
        <span className="min-w-12 text-center text-xs">{Math.round(zoom * 100)}%</span>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={t('preview.action.zoomIn', 'Zoom in')}
          onClick={() => {
            setFitToWidth(false);
            setZoom((value) => Math.min(3, value + 0.25));
          }}
        >
          <Plus className="h-4 w-4" />
        </Button>
        <Button
          variant={fitToWidth ? 'secondary' : 'ghost'}
          size="icon-sm"
          aria-label={t('preview.pdf.fitWidth', 'Fit width')}
          aria-pressed={fitToWidth}
          onClick={() => {
            setFitToWidth(true);
            setZoom(initialPdfPreviewZoom(
              naturalWidth,
              scrollAreaRef.current?.clientWidth ?? 0,
            ));
          }}
        >
          <Maximize2 className="h-4 w-4" />
        </Button>
      </PreviewToolbar>
      <div
        ref={scrollAreaRef}
        className="min-h-0 flex-1 overflow-auto bg-surface-sunken p-4 text-center"
      >
        {loading ? <div className="p-4 text-sm text-muted-foreground">{t('preview.pdf.loading', 'Loading PDF…')}</div> : null}
        <PreviewContextMenu className="mx-auto w-fit" label={t('preview.reference.currentPage', 'Quote current page')}
          build={() => { if (loading || !pageCount) throw new Error('page_not_ready'); return filePageReference(descriptor, pageNumber); }}>
          <canvas ref={canvasRef} className="mx-auto bg-white shadow-sm" data-page-number={pageNumber} />
        </PreviewContextMenu>
      </div>
    </div>
  );
}
