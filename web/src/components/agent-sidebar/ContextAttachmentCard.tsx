import { cn } from '@/lib/utils';
import { attachmentSourceHref } from '@/lib/preview/attachment-source';
import { useChatRenderIdentity } from './chat-render-context';
import { useEffect, useState } from 'react';
import { fetchChatWorkspace } from '@/lib/api/queries/chats';
import { fileRefFromAgentPath } from '@/lib/preview/protocol';
import { standalonePreviewHref } from '@/lib/preview/standalone-preview';
import { FileText, Quote, Shapes, X, ExternalLink } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import {
  contextAttachmentLabel, contextAttachmentSummary, isFileAttachment, type ChatAttachment,
} from './chat-attachments';

/** One card for both a draft and the durable transcript. Large snapshots are
 * mounted only on demand, so history rendering never walks their DOM. */
export function ContextAttachmentCard({ attachment, onRemove, originChatId, compact = false }: {
  attachment: ChatAttachment;
  compact?: boolean;
  onRemove?: () => void;
  originChatId?: string | null;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const identity = useChatRenderIdentity();
  const chatId = originChatId ?? identity?.chatId;
  const legacyPath = isFileAttachment(attachment) ? attachment.path : null;
  const legacyKey = `${chatId ?? ''}:${legacyPath ?? ''}`;
  const [legacySource, setLegacySource] = useState<{ key: string; href?: string; error?: boolean } | null>(null);
  useEffect(() => {
    if (!open || !legacyPath || !chatId) return;
    let disposed = false;
    void fetchChatWorkspace(chatId).then(workspace => {
      const ref = fileRefFromAgentPath(legacyPath, { projectId: workspace.project_id });
      if (!ref) throw new Error('invalid_file_reference');
      if (!disposed) setLegacySource({ key: legacyKey, href: standalonePreviewHref(ref, 'auto', { chatId }) });
    }).catch(() => { if (!disposed) setLegacySource({ key: legacyKey, error: true }); });
    return () => { disposed = true; };
  }, [open, legacyPath, chatId, legacyKey]);
  const sourceHref = attachmentSourceHref(attachment, chatId ? { chatId } : null)
    ?? (legacySource?.key === legacyKey ? legacySource.href : null);
  const label = contextAttachmentLabel(attachment);
  const summary = contextAttachmentSummary(attachment);
  const selector = 'selector' in attachment ? attachment.selector : null;
  const resource = 'resource' in attachment ? attachment.resource : null;
  const filePath = resource?.kind === 'file' ? resource.file_ref.path : 'path' in attachment ? attachment.path : null;
  const extension = filePath?.split('/').pop()?.split('.').slice(1).pop()?.toUpperCase();
  const metadata = [
    resource?.kind === 'workflow' ? resource.version : extension,
    'size_bytes' in attachment && attachment.size_bytes != null
      ? `${(attachment.size_bytes / 1024).toLocaleString(undefined, { maximumFractionDigits: 1 })} KB` : null,
    selector?.kind === 'pages' ? t('composer.context.pagesLabel', { defaultValue: 'Pages {{pages}}', pages: selector.pages.join(', ') }) : null,
    selector?.kind === 'workflow_elements' ? t('composer.context.elementsLabel', {
      defaultValue: '{{nodes}} nodes · {{edges}} edges', nodes: selector.node_ids?.length ?? 0, edges: selector.edges?.length ?? 0,
    }) : null,
  ].filter(Boolean).join(' · ');
  const preview = attachment.type === 'quote' ? attachment.snapshot.text : metadata || summary;
  const Icon = attachment.type === 'quote' ? Quote : attachment.type === 'resource' ? Shapes : FileText;
  return (
    <>
      <div className={cn("preview-click-card relative flex max-w-full shrink-0 border border-edge-subtle bg-surface-sunken/60 text-xs", compact ? "h-8 w-auto max-w-[200px] rounded-lg" : "w-60 rounded-xl")}
        data-role={onRemove ? 'agent-composer-attachment-chip' : 'context-attachment-card'} data-attachment-type={attachment.type}>
        <button type="button" className={cn("flex min-w-0 flex-1 text-left transition-colors hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus", compact ? "items-center gap-1.5 rounded-lg px-2 py-1" : "items-start gap-2.5 rounded-xl px-3 py-2.5")}
          onClick={() => setOpen(true)} aria-label={t('composer.context.view', {defaultValue:'View context: {{name}}', name:label})}>
          <Icon className={cn("shrink-0 text-muted-foreground", compact ? "h-3.5 w-3.5" : "mt-0.5 h-4 w-4")} aria-hidden="true" />
          <span className="min-w-0 flex-1">
            <span className="block truncate font-medium text-foreground" title={label}>{label}</span>
            {!compact && <span className="mt-1 line-clamp-2 break-words text-xs leading-relaxed text-muted-foreground">{preview.slice(0, 320)}</span>}
            {!compact && selector?.kind === 'text' && selector.unsaved && <span className="mt-1 block text-xs font-medium text-amber-700 dark:text-amber-400">{t('composer.context.unsavedText', 'Unsaved text snapshot')}</span>}
          </span>
        </button>
        {onRemove && <button type="button" onClick={onRemove}
          className={cn("mr-1 rounded-full p-1.5 text-muted-foreground hover:bg-background hover:text-foreground focus-visible:ring-2 focus-visible:ring-focus", compact ? "self-center" : "my-1 self-start")}
          aria-label={t('composer.remove_attachment', 'Remove attachment')} data-action="agent-composer-attachment-remove">
          <X className="h-3 w-3" />
        </button>}
      </div>
      {open && <Dialog open onOpenChange={setOpen}>
        <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle className="break-words">{label}</DialogTitle>
            <DialogDescription className="break-all">{attachment.type === 'quote'
              ? t('composer.context.quoted', 'Selected text saved with its source.') : summary}</DialogDescription>
          </DialogHeader>
          <pre className="max-h-[55dvh] overflow-auto whitespace-pre-wrap break-words rounded-lg bg-surface-sunken p-3 text-sm">{
            attachment.type === 'quote' ? attachment.snapshot.text
              : attachment.type === 'resource' && attachment.snapshot ? attachment.snapshot.text : summary
          }</pre>
          {legacyPath && !sourceHref && <p role="status" className="text-xs text-muted-foreground">{!chatId || (legacySource?.key === legacyKey && legacySource.error) ? t('preview.reference.unavailable', 'The source conversation is unavailable or you no longer have access.') : t('preview.reference.checking', 'Checking the source conversation…')}</p>}
          {sourceHref ? <a href={sourceHref} target="_blank" rel="noopener noreferrer" className="inline-flex w-fit items-center rounded-md border px-3 py-2 text-sm hover:bg-surface-hover focus-visible:ring-2 focus-visible:ring-focus"><ExternalLink className="mr-2 h-3.5 w-3.5" aria-hidden="true" />{t('composer.context.openSource', 'Open source')}</a> : null}
          {(filePath || selector || ('source' in attachment)) && <details className="rounded-lg border border-edge-subtle p-3 text-xs text-muted-foreground">
            <summary className="cursor-pointer font-medium">{t('composer.context.referenceDetails', 'Reference details')}</summary>
            {filePath && <p className="mt-2 break-all font-mono">{filePath}</p>}
            {metadata && <p className="mt-2">{metadata}</p>}
            {attachment.type === 'quote' && attachment.source.kind === 'message' && <p className="mt-2 break-all">{t('composer.context.sourceMessage', 'Source message')}: {attachment.source.message_id}</p>}
          </details>}
        </DialogContent>
      </Dialog>}
    </>
  );
}
