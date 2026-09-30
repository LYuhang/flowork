import { Minus, Plus, RotateCcw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { PreviewToolbar } from './PreviewToolbar';
import { useCallback, useMemo, useRef, useState } from 'react';
import FileViewer, { type FileViewerProps, type FileViewerHandle, type ViewerState } from '@file-viewer/react';
import { spreadsheetRenderer } from '@file-viewer/renderer-spreadsheet';
import { useTranslation } from 'react-i18next';
import { useTheme } from 'next-themes';

import { fileRefKey, type PreviewErrorInfo } from '@/lib/preview/protocol';
import { PreviewErrorState } from './PreviewErrorState';
import type { PreviewRendererProps } from './renderer-types';

export function NativeWorkbookPreview({
  descriptor,
  loadAllowed,
}: PreviewRendererProps) {
  const { i18n, t } = useTranslation();
  const viewer = useRef<FileViewerHandle>(null);
  const [viewerState, setViewerState] = useState<ViewerState | null>(null);
  const { resolvedTheme } = useTheme();
  const [renderError, setRenderError] = useState<PreviewErrorInfo | null>(null);
  const url = descriptor.content?.url;
  const viewerKey = `${fileRefKey(descriptor.fileRef)}:${descriptor.revision}`;
  const options = useMemo<NonNullable<FileViewerProps['options']>>(() => ({
    rendererMode: 'replace' as const,
    // The renderer package narrows its mount element to HTMLDivElement while
    // core's public registry accepts HTMLElement. Runtime contracts are the
    // same; isolate the upstream generic-variance mismatch at this boundary.
    renderers: [spreadsheetRenderer] as unknown as NonNullable<FileViewerProps['options']>['renderers'],
    styleIsolation: 'shadow' as const,
    theme: resolvedTheme === 'dark' ? 'dark' as const : 'light' as const,
    i18n: {
      locale: i18n.resolvedLanguage?.toLowerCase().startsWith('zh')
        ? 'zh-CN' as const
        : 'en-US' as const,
    },
    toolbar: {
      items: { 'zoom-in': false, 'zoom-out': false, 'zoom-reset': false },
      download: false,
      print: false,
      exportHtml: false,
      theme: false,
      // This canvas renderer has no search provider; DOM search would always
      // report zero matches even when matching cells exist.
      search: false,
      zoom: true,
    },
    spreadsheet: {
      worker: false,
      resizableColumns: true,
      resizableRows: true,
    },
  }), [i18n.resolvedLanguage, resolvedTheme]);
  const handleStateChange = useCallback((state: ViewerState) => {
    setViewerState(state);
    if (state.error) {
      setRenderError({ code: 'render_failed', params: {} });
    }
  }, []);

  if (!loadAllowed) return null;
  if (!url) {
    return (
      <PreviewErrorState
        descriptor={descriptor}
        error={{ code: 'content_unavailable', params: {} }}
      />
    );
  }
  if (renderError) {
    return <PreviewErrorState descriptor={descriptor} error={renderError} />;
  }

  return (
    <div className="relative h-full min-h-0" onContextMenuCapture={event => {
      if (event.target === event.currentTarget || !(event.target as Element).closest('[data-role="native-workbook-preview"]')) return;
      event.preventDefault(); event.stopPropagation();
      event.currentTarget.dispatchEvent(new MouseEvent('contextmenu', {bubbles:true,cancelable:true,clientX:event.clientX,clientY:event.clientY}));
    }}><PreviewToolbar>
      <Button size="icon-sm" variant="ghost" aria-label={t('preview.action.zoomOut', 'Zoom out')} title={t('preview.action.zoomOut', 'Zoom out')} disabled={!viewerState?.zoom?.canZoomOut} onClick={() => void viewer.current?.zoomOut()}><Minus className="h-3.5 w-3.5" /></Button>
      <span className="min-w-12 text-center text-xs tabular-nums">{viewerState?.zoom?.label ?? '100%'}</span>
      <Button size="icon-sm" variant="ghost" aria-label={t('preview.action.zoomIn', 'Zoom in')} title={t('preview.action.zoomIn', 'Zoom in')} disabled={!viewerState?.zoom?.canZoomIn} onClick={() => void viewer.current?.zoomIn()}><Plus className="h-3.5 w-3.5" /></Button>
      <Button size="icon-sm" variant="ghost" aria-label={t('preview.media.resetZoom', 'Reset zoom')} title={t('preview.media.resetZoom', 'Reset zoom')} disabled={!viewerState?.zoom?.canReset} onClick={() => void viewer.current?.resetZoom()}><RotateCcw className="h-3.5 w-3.5" /></Button>
    </PreviewToolbar>
    <FileViewer
      ref={viewer}
      key={viewerKey}
      className="h-full min-h-0 w-full"
      url={url}
      name={descriptor.name}
      size={descriptor.sizeBytes}
      type={descriptor.name.split('.').pop() ?? 'xlsx'}
      options={options}
      onStateChange={handleStateChange}
      data-role="native-workbook-preview"
    /></div>
  );
}
