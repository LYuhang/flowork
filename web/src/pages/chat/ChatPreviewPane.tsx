import { cn } from '@/lib/utils';
import { PreviewContextMenu } from './preview/PreviewContextMenu';
import { PreviewOriginProvider } from '@/lib/preview/context-origin';
import { lazy, Suspense, useCallback, useRef, useState } from 'react';
import { ChevronDown, MessageSquare, PanelRightClose, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import {
  InteractiveArtifactPreview,
  type SubmitInteractiveAsNewTurn,
} from '@/components/agent-sidebar/tool-render/InteractiveArtifactBlock';
import { AsyncState } from '@/components/ui/async-state';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import type { ChatPreviewItem } from '@/lib/chat/preview-state';
import type { ChatFilePreviewHandle } from './preview/ChatFilePreview';

const ChatFilePreview = lazy(() =>
  import('./preview/ChatFilePreview').then((module) => ({ default: module.ChatFilePreview })),
);
const ChatWorkflowViewer = lazy(() =>
  import('./ChatWorkflowViewer').then((module) => ({ default: module.ChatWorkflowViewer })),
);
const BackgroundJobsPreview = lazy(() =>
  import('./preview/BackgroundJobsPreview').then((module) => ({ default: module.BackgroundJobsPreview })),
);
export interface ChatPreviewPaneProps {
  scopeId: string;
  originChatId?: string | null;
  open: boolean;
  items: ChatPreviewItem[];
  resources: ChatPreviewItem[];
  activeId: string | null;
  onToggleOpen: (open: boolean) => void;
  onSelect: (id: string) => void;
  onOpenResource: (item: ChatPreviewItem) => void;
  onOpenInteractiveFile?: (path: string) => void;
  onCloseItem: (id: string) => void;
  onSubmitInteractiveAsNewMessage?: SubmitInteractiveAsNewTurn;
}

export function ChatPreviewPane({
  scopeId,
  originChatId,
  open,
  items,
  resources,
  activeId,
  onToggleOpen,
  onOpenResource,
  onOpenInteractiveFile,
  onCloseItem,
  onSubmitInteractiveAsNewMessage,
}: ChatPreviewPaneProps) {
  const { t } = useTranslation();
  const [resourcesOpen, setResourcesOpen] = useState(false);
  const selectableResources = [...items, ...resources.filter(resource => !items.some(item => item.id === resource.id))];
  const active = items.find((item) => item.id === activeId) ?? items[0] ?? null;
  const activeInteractive = active?.resource.kind === 'interactive'
    ? active as Extract<ChatPreviewItem, { artifact: unknown }>
    : null;
  const isFullPanePreview = activeInteractive?.artifact.component_type === 'url_preview'
    || activeInteractive?.artifact.component_type === 'workflow_preview';
  const fileViewerRef = useRef<ChatFilePreviewHandle>(null);
  const runWithActiveLeaveGuard = useCallback((action: () => void) => {
    if (active?.resource.kind === 'file' && fileViewerRef.current) {
      fileViewerRef.current.requestLeave(action);
      return;
    }
    action();
  }, [active]);
  const openFileWithActiveLeaveGuard = useCallback((path: string) => {
    if (!onOpenInteractiveFile) return;
    runWithActiveLeaveGuard(() => onOpenInteractiveFile(path));
  }, [onOpenInteractiveFile, runWithActiveLeaveGuard]);

  if (!open) return null;

  const activeTitle = active?.title || t('chat.preview.emptyTitle', 'Preview');
  const activeTypeLabel = active?.resource.kind === 'workflow'
    ? t('chat.preview.type.workflow', 'Workflow')
    : active?.resource.kind === 'file'
      ? t('chat.preview.type.file', 'File')
      : active?.resource.kind === 'interactive'
        ? t('chat.preview.type.interactive', 'Interactive')
        : active?.resource.kind === 'background_jobs'
          ? t('chat.preview.type.backgroundJobs', 'Background jobs')
        : t('chat.preview.type.empty', 'No resource');

  return (
    <PreviewOriginProvider origin={originChatId ? { chatId: originChatId } : null}>
    <aside
      className="chat-preview-pane flex h-full w-full min-w-0 flex-col bg-surface-work"
      data-role="chat-preview-pane"
      aria-label={t('chat.preview.paneLabel', 'Preview')}
    >
      <div className="chat-preview-header chat-pane-header flex h-11 shrink-0 items-center gap-2 px-3">
        <DropdownMenu open={resourcesOpen} onOpenChange={setResourcesOpen}>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-transparent text-muted-foreground transition-colors hover:bg-surface-hover hover:text-foreground"
              aria-label={t('chat.preview.resources', 'Preview resources')}
              title={t('chat.preview.resources', 'Preview resources')}
            >
              <ChevronDown className="h-4 w-4" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-72">
            <DropdownMenuLabel>{t('chat.preview.resources', 'Preview resources')}</DropdownMenuLabel>
            <DropdownMenuSeparator />
            {selectableResources.length === 0 ? (
              <DropdownMenuItem disabled>
                {t('chat.preview.noResources', 'No preview resources yet')}
              </DropdownMenuItem>
            ) : selectableResources.map((item) => (
              <DropdownMenuItem
                key={item.id}
                className="min-w-0"
                onClick={() => {
                  if (item.id === active?.id) return;
                  runWithActiveLeaveGuard(() => onOpenResource(item));
                }}
              >
                <span className="min-w-0 flex-1 truncate">{item.title}</span>
                <span className="shrink-0 text-xs text-muted-foreground">{item.resource.kind}</span>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
        <div className="min-w-0 flex-1" title={activeTitle}>
          <button
            type="button"
            className="flex max-w-full items-center gap-2 text-left"
            onClick={() => setResourcesOpen(true)}
          >
            <span className="truncate text-sm font-medium">{activeTitle}</span>
            <span className="chat-preview-active-type shrink-0 rounded bg-surface-sunken px-1.5 py-0.5 text-xs leading-4 text-content-tertiary">
              {activeTypeLabel}
            </span>
          </button>
        </div>

        {active ? <Button variant="ghost" size="icon-sm"
          title={t('chat.preview.closeItem', 'Close preview item')}
          aria-label={t('chat.preview.closeItem', 'Close preview item')}
          onClick={() => runWithActiveLeaveGuard(() => onCloseItem(active.id))}><X className="h-4 w-4" /></Button> : null}
        <Button
          variant="ghost"
          size="icon"
          className="toolbar-icon-button"
          aria-label={t('chat.preview.closePane', 'Close preview')}
          onClick={() => runWithActiveLeaveGuard(() => onToggleOpen(false))}
        >
          <PanelRightClose className="h-4 w-4" />
        </Button>
      </div>
      <div
        className="min-h-0 flex-1 overflow-hidden bg-surface-work"
        data-role="chat-preview-content"
      >
        {!active ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center text-sm text-muted-foreground">
            <MessageSquare className="h-6 w-6" />
            <div>{t('chat.preview.empty', 'Open a workflow, artifact, or sandbox file to preview it here.')}</div>
          </div>
        ) : active.resource.kind === 'workflow' ? (
          <Suspense fallback={<AsyncState kind="loading" title={t('chat.preview.loadingWorkflow', 'Loading workflow...')} />}>
            <ChatWorkflowViewer workflowId={active.resource.workflowId} onClose={() => onCloseItem(active.id)} />
          </Suspense>
        ) : active.resource.kind === 'file' ? (
          <Suspense fallback={<AsyncState kind="loading" title={t('chat.preview.loadingFile', 'Loading file...')} />}>
            <ChatFilePreview
              ref={fileViewerRef}
              fileRef={active.resource.fileRef}
              onOpenFile={openFileWithActiveLeaveGuard}
            />
          </Suspense>
        ) : active.resource.kind === 'interactive' ? (
          <div className="flex h-full flex-col bg-surface-work">
            <div className={cn('min-h-0 flex-1', isFullPanePreview ? 'overflow-hidden' : 'overflow-auto p-4')}>
              <PreviewContextMenu build={() => {
                if (!activeInteractive?.artifact.artifact_id || !originChatId) throw new Error('artifact_unavailable');
                return { schema_version: 1, id: crypto.randomUUID(), type: 'resource', label: activeInteractive.title,
                  resource: { kind: 'artifact', chat_id: originChatId, artifact_id: activeInteractive.artifact.artifact_id } };
              }} className={cn(
                'overflow-hidden bg-surface-raised',
                isFullPanePreview ? 'h-full min-h-0' : 'rounded-lg border border-edge-subtle',
              )}>
                <InteractiveArtifactPreview
                  artifact={activeInteractive!.artifact}
                  maxHeight={isFullPanePreview ? undefined : 'calc(100vh - 12rem)'}
                  fillAvailableHeight={isFullPanePreview}
                  onSubmitAsNewMessage={onSubmitInteractiveAsNewMessage}
                  onOpenFilePreview={openFileWithActiveLeaveGuard}
                />
              </PreviewContextMenu>
            </div>
          </div>
        ) : (
          <Suspense fallback={<AsyncState kind="loading" title={t('chat.preview.loadingBackgroundTasks', 'Loading background tasks…')} />}>
            <BackgroundJobsPreview
              scopeId={scopeId}
              chatId={active.resource.chatId}
              initialJobId={active.resource.jobId}
              deliveryBatchId={active.resource.deliveryBatchId}
              onOpenFile={onOpenInteractiveFile}
            />
          </Suspense>
        )}
      </div>
    </aside>
    </PreviewOriginProvider>
  );
}
