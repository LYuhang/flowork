import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Copy, Loader2, Share2, ThumbsDown, ThumbsUp } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import {
  copyText, createChatShare, feedbackKey, fetchFeedback, putFeedback,
  sharesKey, shareUrl, type MessageRating,
} from '@/lib/api/chat-engagement';
import { cn } from '@/lib/utils';

export function MessageActions({ chatId, messageId, content }: {
  chatId: string; messageId: string; content: string;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const feedback = useQuery({
    queryKey: feedbackKey(chatId), queryFn: () => fetchFeedback(chatId), staleTime: 30_000,
  });
  const rating = feedback.data?.ratings[messageId] ?? null;
  const [voting, setVoting] = useState(false);
  const [sharing, setSharing] = useState(false);
  const [copied, setCopied] = useState<{ kind: 'text' | 'share' } | null>(null);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(null), 2500);
    return () => clearTimeout(timer);
  }, [copied]);
  const copiedFeedback = (kind: 'text' | 'share') => {
    setCopied({ kind });
  };
  const vote = async (value: 'up' | 'down') => {
    if (voting) return;
    setVoting(true);
    const next = rating === value ? null : value;
    await qc.cancelQueries({ queryKey: feedbackKey(chatId) });
    const update = (value: MessageRating) => qc.setQueryData<{ ratings: Record<string, MessageRating> }>(
      feedbackKey(chatId), (old) => ({ ratings: { ...old?.ratings, [messageId]: value } }),
    );
    update(next);
    try {
      await putFeedback(chatId, messageId, next);
      update(next);
    } catch {
      update(rating);
      toast.error(t('chat.actions.feedbackFailed', 'Could not save feedback. Please try again.'));
    } finally {
      setVoting(false);
    }
  };
  const share = async () => {
    if (sharing) return;
    setSharing(true);
    try {
      const value = await createChatShare(chatId, messageId);
      void qc.invalidateQueries({ queryKey: sharesKey(chatId) });
      await copyText(shareUrl(value));
      copiedFeedback('share');
    } catch {
      toast.error(t('chat.actions.shareFailed', 'Could not copy the share link. Open Share to copy it manually.'));
    } finally {
      setSharing(false);
    }
  };
  const actions = [
    { key: 'copy', label: t('chat.actions.copy', 'Copy response'),
      icon: copied?.kind === 'text' ? Check : Copy, success: copied?.kind === 'text',
      onClick: () => void copyText(content).then(() => copiedFeedback('text')).catch(() => {
        toast.error(t('chat.actions.copyFailed', 'Could not copy. Please try again.'));
      }) },
    { key: 'up', label: t('chat.actions.up', 'Helpful'), icon: ThumbsUp,
      selected: rating === 'up', disabled: voting || feedback.isPending || feedback.isError,
      onClick: () => void vote('up') },
    { key: 'down', label: t('chat.actions.down', 'Not helpful'), icon: ThumbsDown,
      selected: rating === 'down', disabled: voting || feedback.isPending || feedback.isError,
      onClick: () => void vote('down') },
    { key: 'share', label: t('chat.actions.share', 'Share this response — anyone with the link can view'),
      icon: sharing ? Loader2 : copied?.kind === 'share' ? Check : Share2,
      success: copied?.kind === 'share', disabled: sharing, onClick: () => void share() },
  ];
  return (
    <div className="mt-1 flex items-center gap-0.5 text-muted-foreground" data-role="message-actions">
      {actions.map(({ key, label, icon: Icon, selected, success, disabled, onClick }) => (
        <Tooltip key={key}>
          <TooltipTrigger asChild>
            <button type="button" aria-label={label} aria-pressed={selected}
              disabled={disabled} onClick={onClick} data-action={`message-${key}`}
              className={cn('inline-flex h-8 w-8 items-center justify-center rounded-md transition-colors hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-40',
                selected && 'bg-primary/10 text-primary', success && 'text-state-success')}>
              <Icon className={cn('h-3.5 w-3.5', key === 'share' && sharing && 'animate-spin motion-reduce:animate-none')} />
            </button>
          </TooltipTrigger>
          <TooltipContent>{label}</TooltipContent>
        </Tooltip>
      ))}
      <span role="status" className="sr-only">{copied ? t('chat.actions.copied', 'Copied') : ''}</span>
    </div>
  );
}
