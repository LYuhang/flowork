import {
  forwardRef,
  lazy,
  Suspense,
  useCallback,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type ComponentType,
  type LazyExoticComponent,
} from 'react';
import { Download, ExternalLink, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { AsyncState } from '@/components/ui/async-state';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { RendererErrorBoundary } from '@/components/ui/renderer-error-boundary';
import { usePreviewDescriptor } from '@/lib/api/queries/previews';
import { formatBytes } from '@/lib/format/bytes';
import type { FileRefV1, PreviewRendererId } from '@/lib/preview/protocol';
import { standalonePreviewHref } from '@/lib/preview/standalone-preview';
import type { PreviewRendererProps } from './renderer-types';
import { PreviewErrorState } from './PreviewErrorState';
import { routePreviewDescriptor } from './preview-routing';

const TextRenderer = lazy(() => import('./TextPreviewRenderers').then(
  (module) => ({ default: module.TextPreviewRenderer }),
));
const MarkdownRenderer = lazy(() => import('./TextPreviewRenderers').then(
  (module) => ({ default: module.MarkdownPreviewRenderer }),
));
const HtmlRenderer = lazy(() => import('./TextPreviewRenderers').then(
  (module) => ({ default: module.HtmlPreviewRenderer }),
));
const PdfRenderer = lazy(() => import('./PdfPreviewRenderer').then(
  (module) => ({ default: module.PdfPreviewRenderer }),
));
const SpreadsheetRenderer = lazy(() => import('./SpreadsheetPreviewRenderer').then(
  (module) => ({ default: module.SpreadsheetPreviewRenderer }),
));
const MediaRenderer = lazy(() => import('./MediaPreviewRenderer').then(
  (module) => ({ default: module.MediaPreviewRenderer }),
));
const DrawioRenderer = lazy(() => import('./DrawioPreviewRenderer').then(
  (module) => ({ default: module.DrawioPreviewRenderer }),
));
const UnsupportedRenderer = lazy(() => import('./UnsupportedPreviewRenderer').then(
  (module) => ({ default: module.UnsupportedPreviewRenderer }),
));

const rendererRegistry: Record<
  PreviewRendererId,
  LazyExoticComponent<ComponentType<PreviewRendererProps>>
> = {
  text: TextRenderer,
  markdown: MarkdownRenderer,
  html: HtmlRenderer,
  pdf: PdfRenderer,
  docx: PdfRenderer,
  pptx: PdfRenderer,
  spreadsheet: SpreadsheetRenderer,
  image: MediaRenderer,
  audio: MediaRenderer,
  video: MediaRenderer,
  drawio: DrawioRenderer,
  unsupported: UnsupportedRenderer,
};

export interface ChatFilePreviewHandle {
  requestLeave: (onLeave: () => void) => void;
}

export const ChatFilePreview = forwardRef<ChatFilePreviewHandle, {
  fileRef: FileRefV1;
  fileType?: string;
  allowEditing?: boolean;
  allowOpenInNewPage?: boolean;
  onOpenFile?: (path: string) => void;
}>(function ChatFilePreview({
  fileRef,
  fileType = 'auto',
  allowEditing = true,
  allowOpenInNewPage = true,
  onOpenFile,
}, forwardedRef) {
  const { t } = useTranslation();
  const descriptorQuery = usePreviewDescriptor(fileRef);
  const [dirty, setDirty] = useState(false);
  const [leaveDialogOpen, setLeaveDialogOpen] = useState(false);
  const [manualLoadRevision, setManualLoadRevision] = useState<string | null>(null);
  const pendingLeaveRef = useRef<(() => void) | null>(null);
  const resolvedDescriptor = descriptorQuery.data;
  const descriptor = useMemo(
    () => resolvedDescriptor
      ? (() => {
          const routed = routePreviewDescriptor(resolvedDescriptor, fileType);
          return allowEditing
            ? routed
            : {
                ...routed,
                capabilities: { ...routed.capabilities, edit: false },
              };
        })()
      : undefined,
    [allowEditing, fileType, resolvedDescriptor],
  );

  const requestLeave = useCallback((onLeave: () => void) => {
    if (!dirty) {
      onLeave();
      return;
    }
    pendingLeaveRef.current = onLeave;
    setLeaveDialogOpen(true);
  }, [dirty]);
  useImperativeHandle(forwardedRef, () => ({ requestLeave }), [requestLeave]);

  const loadAllowed = useMemo(() => (
    !!descriptor
    && (
      descriptor.loadPolicy !== 'manual'
      || manualLoadRevision === descriptor.revision
    )
  ), [descriptor, manualLoadRevision]);

  if (descriptorQuery.isLoading) {
    return (
      <AsyncState
        kind="loading"
        title={t('preview.resolving', 'Resolving file preview…')}
        className="h-full rounded-none border-0"
      />
    );
  }
  if (descriptorQuery.isError || !descriptor) {
    return (
      <AsyncState
        kind="error"
        title={t('preview.openError.title', 'Unable to open this file')}
        description={descriptorQuery.error?.message}
        actionLabel={t('preview.openError.action', 'Try again')}
        onAction={() => void descriptorQuery.refetch()}
        className="h-full rounded-none border-0"
      />
    );
  }

  const Renderer = rendererRegistry[descriptor.renderer];
  return (
    <div className="flex h-full min-h-0 flex-col bg-surface-work">
      <div className="flex min-h-10 shrink-0 items-center gap-2 border-b border-edge-subtle px-3">
        <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
          {descriptor.contentType} · {formatBytes(descriptor.sizeBytes)}
        </span>
        {allowOpenInNewPage ? (
          <Button asChild variant="ghost" size="sm">
            <a
              href={standalonePreviewHref(descriptor.fileRef, fileType)}
              target="_blank"
              rel="noopener noreferrer"
              aria-label={t('preview.action.openInNewPage', 'Open in new page')}
              title={t('preview.action.openInNewPage', 'Open in new page')}
            >
              <ExternalLink className="mr-1 h-3.5 w-3.5" />
              <span className="hidden sm:inline">
                {t('preview.action.openInNewPage', 'Open in new page')}
              </span>
            </a>
          </Button>
        ) : null}
        {descriptor.capabilities.download && descriptor.content?.url ? (
          <Button asChild variant="ghost" size="sm">
            <a href={descriptor.content.url} download={descriptor.name}>
              <Download className="mr-1 h-3.5 w-3.5" />
              {t('preview.action.download', 'Download')}
            </a>
          </Button>
        ) : null}
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={t('preview.action.refresh', 'Refresh preview')}
          onClick={() => void descriptorQuery.refetch()}
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </Button>
      </div>
      <div className="min-h-0 flex-1 overflow-hidden">
        {descriptor.loadPolicy === 'manual' && !loadAllowed ? (
          <AsyncState
            kind="empty"
            title={t('preview.manual.title', 'Large file preview')}
            description={t(
              'preview.manual.description',
              'This file is not loaded automatically to protect browser memory.',
            )}
            actionLabel={t('preview.manual.action', 'Load preview')}
            onAction={() => setManualLoadRevision(descriptor.revision)}
            className="h-full rounded-none border-0"
          />
        ) : (
          <RendererErrorBoundary
            resetKey={`${descriptor.renderer}:${descriptor.revision}`}
            fallback={(
              <PreviewErrorState
                descriptor={descriptor}
                error={{ code: 'render_failed', params: {} }}
              />
            )}
          >
            <Suspense
              fallback={(
                <AsyncState
                  kind="loading"
                  title={t('preview.renderer.loading', 'Loading renderer…')}
                  className="h-full rounded-none border-0"
                />
              )}
            >
              <Renderer
                descriptor={descriptor}
                loadAllowed={loadAllowed}
                onDirtyChange={setDirty}
                onOpenFile={onOpenFile}
                onReload={() => void descriptorQuery.refetch()}
              />
            </Suspense>
          </RendererErrorBoundary>
        )}
      </div>
      <Dialog open={leaveDialogOpen} onOpenChange={setLeaveDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('preview.leave.title', 'Discard unsaved changes?')}</DialogTitle>
            <DialogDescription>
              {t(
                'preview.leave.description',
                'This file has unsaved edits. Save them first, or discard them before leaving the tab.',
              )}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setLeaveDialogOpen(false)}>
              {t('preview.leave.keepEditing', 'Keep editing')}
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                const action = pendingLeaveRef.current;
                pendingLeaveRef.current = null;
                setDirty(false);
                setLeaveDialogOpen(false);
                action?.();
              }}
            >
              {t('preview.leave.discard', 'Discard')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
});
