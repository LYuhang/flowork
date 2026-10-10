import { useEffect, useState } from 'react';
import { useInfiniteQuery, useQueryClient } from '@tanstack/react-query';
import { Inbox } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { fetchPendingChatActions, type PendingChatAction } from '@/lib/api/chat-engagement';
import { refreshResourceQuery, watchResourceActivity } from '@/lib/api/sse/resource-activity';
import { useFormatDateTime } from '@/lib/timezone';

export type PendingActionFocus = { chatId: string; id: string; revision: number };

/** Shared by the main conversation, canvas and installed extension panels. */
export function PendingActionsMenu({ scopeId, active = true, onSelect }: {
  scopeId: string; active?: boolean; onSelect: (action: PendingChatAction) => void;
}) {
  const { t } = useTranslation();
  const formatTime = useFormatDateTime();
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const actions = useInfiniteQuery({
    queryKey: ['chat-attention', scopeId],
    queryFn: ({ pageParam }) => fetchPendingChatActions(scopeId, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: page => page.next_cursor ?? undefined,
    enabled: open && active && !!scopeId,
    retry: false,
  });
  useEffect(() => {
    // The shared side panel can remain mounted while CSS-hidden.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (!active) setOpen(false);
  }, [active]);
  useEffect(() => {
    if (!open || !active || !scopeId) return;
    return watchResourceActivity(
      `/api/v1/chat-scopes/${encodeURIComponent(scopeId)}/attention/activity`,
      () => refreshResourceQuery(client, ['chat-attention', scopeId]),
    );
  }, [open, active, scopeId, client]);
  const items = actions.data?.pages.flatMap(page => page.items) ?? [];
  return <DropdownMenu open={open} onOpenChange={setOpen}>
    <DropdownMenuTrigger asChild>
      <Button variant="ghost" size="icon" disabled={!scopeId || !active}
        data-action="chat-attention" aria-label={t('chat.attention.title', 'Needs your attention')}
        title={t('chat.attention.title', 'Needs your attention')}>
        <Inbox className="h-4 w-4" />
      </Button>
    </DropdownMenuTrigger>
    <DropdownMenuContent align="end" collisionPadding={8}
      className="max-h-[min(24rem,calc(100vh-4rem))] w-[min(20rem,calc(100vw-1rem))] overflow-y-auto">
      <DropdownMenuLabel>{t('chat.attention.title', 'Needs your attention')}</DropdownMenuLabel>
      <DropdownMenuSeparator />
      {actions.isPending && <p role="status" className="px-2 py-3 text-sm text-muted-foreground">{t('loading', 'Loading…')}</p>}
      {items.map(item => <DropdownMenuItem key={item.hitl_request_id} data-attention-id={item.hitl_request_id}
        className="items-start" onSelect={() => {
          void client.invalidateQueries({ queryKey: ['chats', scopeId] });
          onSelect(item);
        }}>
        <span className="min-w-0 flex-1">
          <span className="block truncate">{item.title || t('chat.attention.untitled', 'Pending request')}</span>
          <span className="mt-1 block text-xs text-muted-foreground">
            {item.hitl_type === 'pre_tool_approval' ? t('chat.attention.approval', 'Approval')
              : item.hitl_type === 'post_tool_review' ? t('chat.attention.review', 'Confirmation')
                : t('chat.attention.question', 'Question')}
            {' · '}<time dateTime={item.created_at}>{formatTime(item.created_at)}</time>
          </span>
        </span>
      </DropdownMenuItem>)}
      {!actions.isPending && !actions.isError && !items.length && !actions.hasNextPage &&
        <p role="status" className="px-2 py-3 text-sm text-muted-foreground">{t('chat.attention.empty', 'Nothing needs your attention.')}</p>}
      {actions.isError && <DropdownMenuItem onSelect={event => {
        event.preventDefault();
        void (actions.isFetchNextPageError ? actions.fetchNextPage() : actions.refetch());
      }}>{t('chat.attention.retry', 'Could not load requests. Retry')}</DropdownMenuItem>}
      {actions.hasNextPage && !actions.isError && <DropdownMenuItem disabled={actions.isFetchingNextPage}
        onSelect={event => { event.preventDefault(); void actions.fetchNextPage(); }}>
        {actions.isFetchingNextPage ? t('loading', 'Loading…') : t('chat.attention.more', 'Load more requests')}
      </DropdownMenuItem>}
    </DropdownMenuContent>
  </DropdownMenu>;
}
