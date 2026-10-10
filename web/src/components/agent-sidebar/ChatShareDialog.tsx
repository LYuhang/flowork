import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Copy, Loader2, Share2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { Markdown } from './Markdown';
import { Input } from '@/components/ui/input';
import { copyText, createChatShare, previewChatShare, fetchChatShares, revokeChatShare, sharesKey, shareUrl } from '@/lib/api/chat-engagement';

export function ChatShareDialog({ chatId, messageId, disabled = false }: { chatId: string; messageId?: string; disabled?: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const shares = useQuery({ queryKey: sharesKey(chatId), queryFn: () => fetchChatShares(chatId), enabled: open && !disabled });
  const selectedShares = shares.data?.items.filter((item) => !messageId || item.message_id === messageId);
  const existingShare = shares.data?.items.find((item) => item.message_id === (messageId ?? null));
  const preview = useQuery({
    queryKey: [...sharesKey(chatId), 'preview', messageId ?? null],
    queryFn: () => previewChatShare(chatId, messageId), enabled: open && !disabled,
    staleTime: 0,
  });
  const perform = async (key: string, action: () => Promise<unknown>) => {
    setBusy(key);
    try {
      await action();
      await qc.invalidateQueries({ queryKey: sharesKey(chatId) });
    } catch {
      toast.error(t('chat.share.failed', 'Sharing failed. Please try again.'));
    } finally { setBusy(null); }
  };
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant={messageId ? "ghost" : "outline"} size={messageId ? "icon" : "sm"} className={messageId ? "h-8 w-8" : undefined} disabled={disabled} data-action={messageId ? "message-share" : "chat-share"}
          aria-label={messageId ? t('chat.actions.share', 'Share this response — anyone with the link can view') : t('chat.share.title', 'Share conversation')}>
          <Share2 className="h-4 w-4" />{!messageId && <span className="hidden sm:inline">{t('chat.share.button', 'Share')}</span>}
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{messageId ? t('chat.share.response', 'Single response') : t('chat.share.title', 'Share conversation')}</DialogTitle>
          <DialogDescription>{t('chat.share.description', 'Anyone with the link can read this snapshot. Later messages are not added. Links stay available until you revoke them.')}</DialogDescription>
        </DialogHeader>
        <p className="text-sm text-muted-foreground">{t('chat.share.exclusions', 'Includes completed message text only. Private files, attachments and tool logs are not shared.')}</p>
        {shares.isPending || preview.isPending ? <p role="status">{t('common.loading', 'Loading')}</p> : shares.isError || preview.isError ? (
          <Button variant="outline" onClick={() => void Promise.all([shares.refetch(), preview.refetch()])}>{t('common.retry', 'Retry')}</Button>
        ) : (
          <div className="max-h-[50vh] space-y-4 overflow-y-auto">
            <section className="space-y-2 rounded-md border border-edge-subtle bg-muted/30 p-3" aria-label={t('chat.share.preview', 'Share preview')}>
              <p className="text-xs text-muted-foreground">{preview.data?.existing ? t('chat.share.existingSnapshot', 'Preview of the existing link. Revoke it to create a new snapshot.') : t('chat.share.previewNotice', 'Preview of completed text. The snapshot is captured when you create the link.')}</p>
              {preview.data?.messages.map((message) => <div key={message.id} className="border-b border-edge-subtle pb-2 last:border-0">
                <Markdown publicView>{message.content}</Markdown>
              </div>)}
            </section>
            {!existingShare && <Button disabled={busy !== null} onClick={() => void perform('create', () => createChatShare(chatId, messageId))}>
              {busy === 'create' ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <Share2 className="h-4 w-4" />}
              {messageId ? t('chat.share.createResponse', 'Create response link') : t('chat.share.create', 'Create conversation link')}
            </Button>}
            {selectedShares?.map((share) => (
              <div key={share.id} className="space-y-2 border-t border-edge-subtle pt-3">
                <div className="flex items-center justify-between gap-2 text-sm">
                  <span>{share.message_id ? t('chat.share.response', 'Single response') : t('chat.share.conversation', 'Conversation')} · {share.message_count} {t('chat.share.messages', 'messages')}</span>
                  <span className="text-xs text-muted-foreground">{new Date(share.created_at).toLocaleDateString()}</span>
                </div>
                <div className="flex gap-2">
                  <Input readOnly value={shareUrl(share)} aria-label={t('chat.share.link', 'Share link')} onFocus={(event) => event.target.select()} />
                  <Button variant="outline" size="icon" aria-label={t('chat.share.copy', 'Copy link')} onClick={() => void copyText(shareUrl(share)).then(() => setCopied(share.id)).catch(() => toast.error(t('chat.actions.copyFailed', 'Could not copy. Please try again.')))}>
                    {copied === share.id ? <Check className="h-4 w-4 text-state-success" /> : <Copy className="h-4 w-4" />}
                  </Button>
                </div>
                <Button variant="ghost" size="sm" className="text-destructive" disabled={busy !== null}
                  onClick={() => void perform(share.id, () => revokeChatShare(chatId, share.id))}>
                  {t('chat.share.revoke', 'Revoke link')}
                </Button>
              </div>
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
