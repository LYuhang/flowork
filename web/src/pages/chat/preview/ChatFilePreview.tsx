import { PreviewToolbarHost } from './preview-toolbar-context';
import { PreviewContextMenu } from './PreviewContextMenu';
import { usePreviewOrigin } from '@/lib/preview/context-origin';
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
import { PreviewFileActions } from './PreviewFileActions';
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
  expectedRevision?: string | null;
  initialPage?: number;
  allowEditing?: boolean;
  allowOpenInNewPage?: boolean;
  onOpenFile?: (path: string) => void;
}>(function ChatFilePreview({
  fileRef,
  fileType = 'auto',
  expectedRevision,
  initialPage,
  allowEditing = true,
  allowOpenInNewPage = true,
  onOpenFile,
}, forwardedRef) {
  const { t } = useTranslation();
  const origin = usePreviewOrigin();
  const descriptorQuery = usePreviewDescriptor(fileRef);
  const [toolbarHost, setToolbarHost] = useState<HTMLDivElement | null>(null);
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

  if (expectedRevision && descriptor.revision !== expectedRevision) return <AsyncState kind="error"
    title={t('preview.reference.fileChanged', 'The source file has changed since this reference was created.')}
    description={t('preview.reference.fileChangedHelp', 'The saved excerpt remains in the conversation. This preview cannot reproduce the original file version.')} />;
  const Renderer = rendererRegistry[descriptor.renderer];
  return (
    <PreviewToolbarHost.Provider value={toolbarHost}><div className="preview-file-container flex h-full min-h-0 flex-col bg-surface-work">
      <div data-role="preview-file-toolbar" className="flex h-10 shrink-0 items-center gap-1 border-b border-edge-subtle px-2">
        <div ref={setToolbarHost} className="min-w-0 flex-1 overflow-x-auto" />
        <span className="preview-file-metadata max-w-36 truncate text-xs text-muted-foreground">
          {descriptor.contentType} · {formatBytes(descriptor.sizeBytes)}
        </span>

        <PreviewFileActions
          openHref={allowOpenInNewPage ? standalonePreviewHref(descriptor.fileRef, fileType, origin) : undefined}
          downloadHref={descriptor.capabilities.download ? descriptor.content?.url ?? undefined : undefined}
          filename={descriptor.name}
          onRefresh={() => requestLeave(() => { void descriptorQuery.refetch(); })}
        />
      </div>
      <PreviewContextMenu className="min-h-0 flex-1 overflow-hidden" build={() => ({
        schema_version: 1, id: crypto.randomUUID(), type: 'file', label: descriptor.name,
        resource: { kind: 'file', file_ref: descriptor.fileRef, revision: descriptor.revision },
        content_type: descriptor.contentType, size_bytes: descriptor.sizeBytes,
      })}>
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
                initialPage={initialPage}
                descriptor={descriptor}
                loadAllowed={loadAllowed}
                onDirtyChange={setDirty}
                onOpenFile={onOpenFile}
                onReload={() => void descriptorQuery.refetch()}
              />
            </Suspense>
          </RendererErrorBoundary>
        )}
      </PreviewContextMenu>
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
    </div></PreviewToolbarHost.Provider>
  );
});
