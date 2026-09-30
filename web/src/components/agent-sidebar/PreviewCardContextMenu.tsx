import { useRef, useState, type ReactElement } from 'react';
import { Quote } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem } from '@/components/ui/context-menu';
import { addContextToChat } from '@/lib/api/context-draft';
import { fetchChatWorkspace } from '@/lib/api/queries/chats';
import { resolvePreview } from '@/lib/api/previews';
import { standalonePreviewTarget } from '@/lib/preview/standalone-preview';
import type { ChatAttachment } from './chat-attachments';

export function PreviewCardContextMenu({ children, chatId, artifactId, filePath, workflow, label }: {
  children: ReactElement; chatId?: string | null; artifactId?: string | null;
  filePath?: string; workflow?: { id: string; version: string }; label: string;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const pending = useRef<{ identity: string; attachment: ChatAttachment } | null>(null);
  const hasSource = filePath || (workflow?.id && /^v[1-9]\d*\.sv\d+$/.test(workflow.version)) || artifactId;
  const disabled = busy || !chatId || !hasSource;
  const quote = async () => {
    if (disabled || !chatId) return;
    setBusy(true);
    try {
      const identity = JSON.stringify({ chatId, artifactId, filePath, workflow });
      if (pending.current?.identity !== identity) {
        const base = { schema_version: 1 as const, id: crypto.randomUUID(), label };
        let attachment: ChatAttachment;
        if (filePath) {
          const workspace = await fetchChatWorkspace(chatId);
          const query = new URLSearchParams({ scope: filePath.startsWith('/mount/') ? 'mount' : 'project', path: filePath });
          if (workspace.project_id) query.set('projectId', workspace.project_id);
          const target = standalonePreviewTarget(query);
          if (!target) throw new Error('invalid_file_reference');
          const descriptor = await resolvePreview(target.fileRef);
          attachment = { ...base, type: 'file', content_type: descriptor.contentType,
            resource: { kind: 'file', file_ref: descriptor.fileRef, revision: descriptor.revision } };
        } else if (workflow?.id && /^v[1-9]\d*\.sv\d+$/.test(workflow.version)) {
          attachment = { ...base, type: 'resource', resource: { kind: 'workflow', workflow_id: workflow.id, version: workflow.version } };
        } else {
          attachment = { ...base, type: 'resource', resource: { kind: 'artifact', chat_id: chatId, artifact_id: artifactId! } };
        }
        pending.current = { identity, attachment };
      }
      await addContextToChat(chatId, [pending.current.attachment], pending.current.attachment.id!);
      pending.current = null;
      toast.success(t('composer.context.added', 'Added to the conversation draft'));
    } catch {
      toast.error(t('composer.context.addFailed', 'Could not add this reference. Its source may be unavailable; retry without losing your selection.'));
    } finally { setBusy(false); }
  };
  return <ContextMenu><ContextMenuTrigger asChild>{children}</ContextMenuTrigger>
    <ContextMenuContent>
      <ContextMenuItem disabled={disabled} onSelect={() => void quote()}>
        <Quote className="mr-2 h-4 w-4" />{t('composer.context.quote', 'Quote in conversation')}
      </ContextMenuItem>
      {!chatId || !hasSource ? <p className="max-w-64 px-2 py-1 text-xs text-muted-foreground">{t('preview.reference.unavailable', 'The source conversation is unavailable or you no longer have access.')}</p> : null}
    </ContextMenuContent>
  </ContextMenu>;
}
