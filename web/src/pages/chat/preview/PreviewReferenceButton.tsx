import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Quote } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { ContextMenuItem } from '@/components/ui/context-menu';
import { Button } from '@/components/ui/button';
import type { ChatAttachment } from '@/components/agent-sidebar/chat-attachments';
import { addContextToChat, fetchContextDraft } from '@/lib/api/context-draft';
import { usePreviewOrigin } from '@/lib/preview/context-origin';

/** Shared authorization and durable append, independent of the currently active chat. */
interface ReferenceButtonProps {
  build: () => ChatAttachment;
  disabledReason?: string;
  label?: string;
  menu?: boolean;
}
export function PreviewReferenceButton(props: ReferenceButtonProps) {
  const origin = usePreviewOrigin();
  const { t } = useTranslation();
  if (!origin) {
    const reason = t('preview.reference.noOrigin', 'Open this preview from a conversation to add a reference.');
    if (props.menu) return <ContextMenuItem disabled title={reason}><Quote className="mr-2 h-4 w-4" />{props.label || t('composer.context.quote', 'Quote in conversation')}</ContextMenuItem>;
  return <span title={reason} className="inline-flex shrink-0"><Button type="button" size="sm" variant="ghost" disabled aria-label={reason} data-action="preview-reference">
      <Quote className="h-3.5 w-3.5 sm:mr-1.5" /><span className="hidden sm:inline">{props.label || t('composer.context.quote', 'Quote in conversation')}</span>
    </Button></span>;
  }
  return <AuthorizedReferenceButton {...props} />;
}
function AuthorizedReferenceButton({ build, disabledReason, label, menu }: ReferenceButtonProps) {
  const { t } = useTranslation();
  const origin = usePreviewOrigin();
  const [busy, setBusy] = useState(false);
  const pending = useRef<{ identity: string; attachment: ChatAttachment; operationId: string } | null>(null);
  const access = useQuery({
    queryKey: ['preview-origin-access', origin?.chatId],
    queryFn: () => fetchContextDraft(origin!.chatId),
    enabled: !!origin?.chatId, staleTime: 30_000, retry: false,
  });
  const reason = disabledReason || (!origin
    ? t('preview.reference.noOrigin', 'Open this preview from a conversation to add a reference.')
    : access.isError ? t('preview.reference.unavailable', 'The source conversation is unavailable or you no longer have access.')
    : access.isPending ? t('preview.reference.checking', 'Checking the source conversation…') : undefined);
  const add = async () => {
    if (busy || reason || !origin) return;
    setBusy(true);
    try {
      const candidate = build();
      const identity = JSON.stringify({ ...candidate, id: undefined, chatId: origin.chatId });
      if (pending.current?.identity !== identity) pending.current = { identity, attachment: candidate, operationId: crypto.randomUUID() };
      await addContextToChat(origin.chatId, [pending.current.attachment], pending.current.operationId);
      pending.current = null;
      toast.success(t('composer.context.added', 'Added to the conversation draft'));
    } catch {
      toast.error(t('composer.context.addFailed', 'Could not add this reference. Its source may be unavailable; retry without losing your selection.'));
      void access.refetch();
    } finally { setBusy(false); }
  };
  if (menu) return <ContextMenuItem disabled={busy || !!reason} title={reason} onSelect={() => void add()}><Quote className="mr-2 h-4 w-4" />{label || t('composer.context.quote', 'Quote in conversation')}</ContextMenuItem>;
  return <span title={reason} className="inline-flex shrink-0">
    <Button type="button" size="sm" variant="ghost" disabled={busy || !!reason}
      aria-label={reason || label || t('composer.context.quote', 'Quote in conversation')}
      onClick={() => void add()} data-action="preview-reference">
      <Quote className="h-3.5 w-3.5 sm:mr-1.5" />
      <span className="hidden sm:inline">{label || t('composer.context.quote', 'Quote in conversation')}</span>
    </Button>
  </span>;
}
