import { useCallback, useEffect, useRef } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { markChatTurnRead } from '@/lib/api/chat-engagement';
import type { ChatSessionsPage } from '@/lib/api/queries/chats';

/** Mounted at the durable transcript tail only when this completed turn is loaded. */
export function CompletedTurnReadMarker({ scopeId, chatId, runId }: {
  scopeId: string; chatId: string; runId: string;
}) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const marker = useRef<HTMLDivElement>(null);
  const attempted = useRef(false);
  const visible = useRef(false);
  const { mutate, isError, isPending } = useMutation({
    mutationFn: () => markChatTurnRead(chatId, runId),
    retry: false,
    onSuccess: () => {
      client.setQueriesData<ChatSessionsPage>({ queryKey: ['chats', scopeId] }, old => old && ({
        ...old, items: old.items.map(chat => chat.chat_id === chatId && chat.latest_run?.run_id === runId
          ? { ...chat, latest_run: { ...chat.latest_run, unread: false } } : chat),
      }));
      void client.invalidateQueries({ queryKey: ['chats', scopeId] });
    },
  });
  const acknowledge = useCallback(() => {
    if (!visible.current || document.visibilityState !== 'visible' || attempted.current) return;
    attempted.current = true;
    mutate();
  }, [mutate]);
  useEffect(() => {
    if (!marker.current) return;
    const observer = new IntersectionObserver(entries => {
      visible.current = entries.some(entry => entry.isIntersecting);
      acknowledge();
    });
    observer.observe(marker.current);
    document.addEventListener('visibilitychange', acknowledge);
    return () => {
      observer.disconnect();
      document.removeEventListener('visibilitychange', acknowledge);
    };
  }, [acknowledge]);
  return <div ref={marker} className="min-h-px" data-chat-read-marker={runId}>
    {isError && <Button type="button" variant="quiet" size="sm" className="h-auto min-h-8 whitespace-normal text-left text-xs underline [@media(pointer:coarse)]:min-h-11" disabled={isPending}
      onClick={() => { attempted.current = false; acknowledge(); }}>
      {t('chat.read.retry', 'Could not update read status. Retry')}
    </Button>}
  </div>;
}
