import { PreviewToolbar } from './PreviewToolbar';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Download, ExternalLink, Focus, ZoomIn, ZoomOut } from 'lucide-react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';

import { AsyncState } from '@/components/ui/async-state';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import type { PreviewRendererProps } from './renderer-types';
import { PreviewErrorState } from './PreviewErrorState';
import {
  exportDrawioXml,
  openDrawioEditor,
  renderDrawioXml,
  type DrawioExportFormat,
} from './drawio-export';

interface DrawioPreviewRendererProps extends PreviewRendererProps {
  fitRequest?: number;
  surface?: 'inline' | 'full';
}

interface ViewTransform {
  scale: number;
  x: number;
  y: number;
}

const INITIAL_VIEW: ViewTransform = { scale: 1, x: 0, y: 0 };

function clampScale(scale: number): number {
  return Math.min(4, Math.max(0.1, scale));
}

export function DrawioPreviewRenderer({
  descriptor,
  fitRequest = 0,
  surface = 'full',
  onReload,
}: DrawioPreviewRendererProps) {
  const { t } = useTranslation();
  const [retry, setRetry] = useState(0);
  const resourceUrl = descriptor.content?.url ?? null;
  const viewportRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{
    pointerId: number;
    clientX: number;
    clientY: number;
    viewX: number;
    viewY: number;
  } | null>(null);
  const [source, setSource] = useState<{
    url: string;
    xml: string | null;
    failed: boolean;
  } | null>(null);
  const [exporting, setExporting] = useState<DrawioExportFormat | null>(null);
  const [rendered, setRendered] = useState<{
    xml: string;
    url: string | null;
    failed: boolean;
  } | null>(null);
  const [imageSize, setImageSize] = useState<{ url: string; width: number; height: number } | null>(null);
  const [view, setView] = useState<ViewTransform>(INITIAL_VIEW);

  useEffect(() => {
    const controller = new AbortController();
    if (!resourceUrl) return () => controller.abort();
    void fetch(resourceUrl, { signal: controller.signal, cache: 'no-store' })
      .then((response) => {
        if (!response.ok) throw new Error(`draw.io source request failed: ${response.status}`);
        return response.text();
      })
      .then((xml) => setSource({ url: resourceUrl, xml, failed: false }))
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === 'AbortError')) {
          setSource({ url: resourceUrl, xml: null, failed: true });
        }
      });
    return () => controller.abort();
  }, [descriptor.revision, resourceUrl, retry]);

  const currentSource = source?.url === resourceUrl ? source : null;
  const xml = currentSource?.xml ?? null;
  const failed = !resourceUrl || currentSource?.failed === true;

  const invalid = descriptor.diagram?.status === 'invalid';
  const exportDiagram = useCallback(async (format: DrawioExportFormat) => {
    if (!xml || exporting) return;
    setExporting(format);
    try {
      await exportDrawioXml(xml, descriptor.name, format);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Unable to export diagram');
    } finally {
      setExporting(null);
    }
  }, [descriptor.name, exporting, xml]);

  useEffect(() => {
    if (!xml) return undefined;
    let disposed = false;
    let objectUrl: string | null = null;
    void renderDrawioXml(xml, 'svg')
      .then((blob) => {
        objectUrl = URL.createObjectURL(blob);
        if (disposed) {
          URL.revokeObjectURL(objectUrl);
          return;
        }
        setRendered({ xml, url: objectUrl, failed: false });
      })
      .catch(() => {
        if (!disposed) setRendered({ xml, url: null, failed: true });
      });
    return () => {
      disposed = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [xml, retry]);

  const currentRender = rendered?.xml === xml ? rendered : null;
  const currentImageSize = imageSize?.url === currentRender?.url ? imageSize : null;

  const fitToViewport = useCallback((width?: number, height?: number) => {
    const viewport = viewportRef.current;
    const naturalWidth = width ?? currentImageSize?.width;
    const naturalHeight = height ?? currentImageSize?.height;
    if (!viewport || !naturalWidth || !naturalHeight) return;
    const padding = surface === 'inline' ? 20 : 48;
    const availableWidth = Math.max(1, viewport.clientWidth - padding * 2);
    const availableHeight = Math.max(1, viewport.clientHeight - padding * 2);
    const scale = clampScale(Math.min(availableWidth / naturalWidth, availableHeight / naturalHeight));
    setView({
      scale,
      x: (viewport.clientWidth - naturalWidth * scale) / 2,
      y: (viewport.clientHeight - naturalHeight * scale) / 2,
    });
  }, [currentImageSize?.height, currentImageSize?.width, surface]);

  useEffect(() => {
    if (!currentImageSize) return undefined;
    const animationFrame = window.requestAnimationFrame(() => fitToViewport());
    return () => window.cancelAnimationFrame(animationFrame);
  }, [currentImageSize, fitRequest, fitToViewport]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || !currentImageSize) return undefined;
    const observer = new ResizeObserver(() => fitToViewport());
    observer.observe(viewport);
    return () => observer.disconnect();
  }, [currentImageSize, fitToViewport]);

  const zoomAtCenter = useCallback((factor: number) => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    setView((current) => {
      const nextScale = clampScale(current.scale * factor);
      const centerX = viewport.clientWidth / 2;
      const centerY = viewport.clientHeight / 2;
      return {
        scale: nextScale,
        x: centerX - ((centerX - current.x) * nextScale) / current.scale,
        y: centerY - ((centerY - current.y) * nextScale) / current.scale,
      };
    });
  }, []);

  const retryPreview = () => {
    setSource(null); setRendered(null); setRetry(value => value + 1); onReload?.();
  };
  if (failed) {
    return <PreviewErrorState descriptor={descriptor} error={{ code: 'content_unavailable', params: {} }} onRetry={retryPreview} />;
  }
  if (invalid) {
    return <PreviewErrorState descriptor={descriptor} error={{ code: 'invalid_file', params: {} }} />;
  }
  if (currentRender?.failed) {
    return <PreviewErrorState descriptor={descriptor} error={{ code: 'render_failed', params: {} }}
      message={t('preview.drawio.renderUnavailable', 'The file was read, but the draw.io renderer could not finish. Check your connection to diagrams.net and try again.')} onRetry={retryPreview} />;
  }
  if (!currentRender?.url || !xml) {
    return <AsyncState kind="loading" title={t('preview.drawio.loading', 'Loading draw.io preview…')} className="h-full rounded-none border-0" />;
  }

  return (
    <div className="relative h-full min-h-72 w-full bg-white" data-role="drawio-preview">
      {surface === 'full' ? (
        <PreviewToolbar>
          <Button size="icon-sm" variant="ghost" aria-label={t('preview.action.zoomOut', 'Zoom out')} title={t('preview.action.zoomOut', 'Zoom out')} onClick={() => zoomAtCenter(0.8)}>
            <ZoomOut className="h-3.5 w-3.5" />
          </Button>
          <Button size="icon-sm" variant="ghost" aria-label={t('preview.action.zoomIn', 'Zoom in')} title={t('preview.action.zoomIn', 'Zoom in')} onClick={() => zoomAtCenter(1.25)}>
            <ZoomIn className="h-3.5 w-3.5" />
          </Button>
          <Button size="icon-sm" variant="ghost" title={t('preview.drawio.fit', 'Fit diagram')} aria-label={t('preview.drawio.fit', 'Fit diagram')} onClick={() => fitToViewport()}>
            <Focus className="h-3.5 w-3.5" />
          </Button>
          <Button
            size="icon-sm"
            variant="ghost"
            title={t('preview.drawio.continueEditing', 'Continue editing in draw.io')} aria-label={t('preview.drawio.continueEditing', 'Continue editing in draw.io')}
            onClick={() => {
              void openDrawioEditor(xml, descriptor.name).catch((error: unknown) => {
                toast.error(error instanceof Error
                  ? error.message
                  : t('preview.drawio.openFailed', 'Unable to open draw.io'));
              });
            }}
          >
            <ExternalLink className="h-3.5 w-3.5" />
          </Button>
          {descriptor.capabilities.download ? (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button size="icon-sm" variant="ghost" disabled={exporting !== null} title={t('common.export', 'Export')} aria-label={t('common.export', 'Export')}>
                  <Download className="h-3.5 w-3.5" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onSelect={() => void exportDiagram('drawio')}>
                  {t('preview.drawio.export.native', 'draw.io · native source')}
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => void exportDiagram('svg')}>
                  {t('preview.drawio.export.svg', 'SVG · scalable vector')}
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => void exportDiagram('png')}>
                  {t('preview.drawio.export.png', 'PNG · image with embedded source')}
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => void exportDiagram('pdf')}>
                  {t('preview.drawio.export.pdf', 'PDF · document and print')}
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => void exportDiagram('jpg')}>
                  {t('preview.drawio.export.jpg', 'JPG · compact bitmap')}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          ) : null}
        </PreviewToolbar>
      ) : null}
      <div
        ref={viewportRef}
        role="img"
        aria-label={descriptor.name}
        data-role="drawio-canvas"
        className="relative h-full min-h-72 w-full touch-none overflow-hidden bg-white cursor-grab active:cursor-grabbing"
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          dragRef.current = {
            pointerId: event.pointerId,
            clientX: event.clientX,
            clientY: event.clientY,
            viewX: view.x,
            viewY: view.y,
          };
          event.currentTarget.setPointerCapture(event.pointerId);
        }}
        onPointerMove={(event) => {
          const drag = dragRef.current;
          if (!drag || drag.pointerId !== event.pointerId) return;
          setView((current) => ({
            ...current,
            x: drag.viewX + event.clientX - drag.clientX,
            y: drag.viewY + event.clientY - drag.clientY,
          }));
        }}
        onPointerUp={(event) => {
          if (dragRef.current?.pointerId === event.pointerId) dragRef.current = null;
        }}
        onPointerCancel={() => { dragRef.current = null; }}
        onWheel={(event) => {
          event.preventDefault();
          const bounds = event.currentTarget.getBoundingClientRect();
          const pointerX = event.clientX - bounds.left;
          const pointerY = event.clientY - bounds.top;
          const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
          setView((current) => {
            const nextScale = clampScale(current.scale * factor);
            return {
              scale: nextScale,
              x: pointerX - ((pointerX - current.x) * nextScale) / current.scale,
              y: pointerY - ((pointerY - current.y) * nextScale) / current.scale,
            };
          });
        }}
      >
        <img
          key={currentRender.url}
          src={currentRender.url}
          alt=""
          draggable={false}
          className="pointer-events-none absolute left-0 top-0 max-w-none select-none"
          style={{
            transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`,
            transformOrigin: '0 0',
          }}
          onLoad={(event) => {
            const width = event.currentTarget.naturalWidth;
            const height = event.currentTarget.naturalHeight;
            setImageSize({ url: currentRender.url!, width, height });
            fitToViewport(width, height);
          }}
        />
      </div>
    </div>
  );
}
