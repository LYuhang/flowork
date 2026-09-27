import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Copy, Loader2, Share2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { copyText, createChatShare, fetchChatShares, revokeChatShare, sharesKey, shareUrl } from '@/lib/api/chat-engagement';

export function ChatShareDialog({ chatId, disabled = false }: { chatId: string; disabled?: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const shares = useQuery({ queryKey: sharesKey(chatId), queryFn: () => fetchChatShares(chatId), enabled: open && !disabled });
  const conversationShare = shares.data?.items.find((item) => item.message_id === null);
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
        <Button variant="outline" size="sm" disabled={disabled} data-action="chat-share"
          aria-label={t('chat.share.title', 'Share conversation')}>
          <Share2 className="h-4 w-4" /><span className="hidden sm:inline">{t('chat.share.button', 'Share')}</span>
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t('chat.share.title', 'Share conversation')}</DialogTitle>
          <DialogDescription>{t('chat.share.description', 'Anyone with the link can read this snapshot. Later messages are not added. Links stay available until you revoke them.')}</DialogDescription>
        </DialogHeader>
        <p className="text-sm text-muted-foreground">{t('chat.share.exclusions', 'Includes completed message text only. Private files, attachments and tool logs are not shared.')}</p>
        {shares.isPending ? <p role="status">{t('common.loading', 'Loading')}</p> : shares.isError ? (
          <Button variant="outline" onClick={() => void shares.refetch()}>{t('common.retry', 'Retry')}</Button>
        ) : (
          <div className="max-h-[50vh] space-y-4 overflow-y-auto">
            {!conversationShare && <Button disabled={busy !== null} onClick={() => void perform('create', () => createChatShare(chatId))}>
              {busy === 'create' ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <Share2 className="h-4 w-4" />}
              {t('chat.share.create', 'Create conversation link')}
            </Button>}
            {shares.data?.items.map((share) => (
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
