/**
 * Header History affordance: a DropdownMenu of the workflow's chat sessions.
 * `useChatSessions` is called at the top level (not inside the menu content)
 * so it fetches on mount, not on first open (radix mounts content lazily).
 */
import { useEffect, useState } from 'react';
import { History, MoreHorizontal, Pin } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuSeparator,
  DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent, DropdownMenuPortal,
} from '@/components/ui/dropdown-menu';
import { useChatSessions } from '@/lib/api/queries/chats';
import { useFormatDateTime } from '@/lib/timezone';
import { useChatListActions } from './use-chat-list-actions';
import { ChatRunStatus } from './ChatRunStatus';

export interface ChatHistoryMenuProps {
  wfId: string;
  activeChatId: string | null;
  onSelect: (chatId: string) => void;
  onIntent?: (chatId: string) => void;
  surface?: 'chat' | 'browser';
  /** Close portalled menu content while its parent panel is CSS-hidden. */
  active?: boolean;
  workflowContextOnly?: boolean;
}

export function ChatHistoryMenu({
  wfId,
  activeChatId,
  onSelect,
  onIntent,
  surface = 'chat',
  active = true,
  workflowContextOnly = false,
}: ChatHistoryMenuProps) {
  const { t } = useTranslation();
  const formatDateTime = useFormatDateTime();
  const [open, setOpen] = useState(false);
  const [view, setView] = useState('active');
  const actionsFor = useChatListActions();
  const sessions = useChatSessions(wfId, surface);
  const items = (sessions.data?.items ?? []).filter(item =>
    Boolean(item.archived_at) === (view === 'archived')
    && (!workflowContextOnly || item.workflow_context?.workflow_id === wfId));

  useEffect(() => {
    // Closing synchronously prevents portalled menu content remaining visible
    // when its parent panel is hidden.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (!active) setOpen(false);
  }, [active]);

  return (
    <DropdownMenu open={open} onOpenChange={(next) => {
      setOpen(next);
      // Pick up conversations created in another canvas window.
      if (next) void sessions.refetch();
    }}>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          aria-label={t('chat_history', 'Chat History')}
          data-action="agent-sidebar-history"
          disabled={!active}
        >
          <History className="h-4 w-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        collisionPadding={8}
        className="max-h-[min(24rem,calc(100vh-4rem))] w-[min(20rem,calc(100vw-1rem))] overflow-y-auto"
      >
        <DropdownMenuRadioGroup value={view} onValueChange={setView}>
          <DropdownMenuRadioItem value="active" onSelect={event => event.preventDefault()}>{t('chat.list.active', 'Current chats')}</DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="archived" onSelect={event => event.preventDefault()}>{t('chat.list.archived', 'Archived chats')}</DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        {sessions.isLoading ? (
          <div className="space-y-1 p-1">
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-6 w-full" />
          </div>
        ) : items.length ? (
          items.map((s) => {
            const label = s.chat_context || s.chat_id.slice(0, 8);
            const context = s.workflow_context;
            const target = context?.target;
            const contextLabel = context
              ? [`v${context.major_version}.sv${context.initial_subversion}`,
                target?.kind === 'node' ? target.node_id
                  : target?.kind === 'edge' ? `${target.source} → ${target.target}`
                    : context.workflow_id].filter(Boolean).join(' · ')
              : null;
            const activityAt = s.last_message_at ?? s.updated_at ?? s.created_at;
            return (
              <div key={s.chat_id} className="flex min-w-0 items-center">
                <DropdownMenuItem
                  data-chat-id={s.chat_id}
                  aria-current={s.chat_id === activeChatId ? 'true' : undefined}
                  onSelect={() => onSelect(s.chat_id)}
                  onPointerEnter={() => onIntent?.(s.chat_id)}
                  onFocus={() => onIntent?.(s.chat_id)}
                  className={s.chat_id === activeChatId ? 'min-w-0 flex-1 bg-accent text-accent-foreground' : 'min-w-0 flex-1'}
                  title={label}
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate whitespace-nowrap">{label}</span>
                    {contextLabel && (
                      <span className="mt-0.5 block space-y-0.5 text-xs text-muted-foreground">
                        <span className="block truncate" title={contextLabel}>{contextLabel}</span>
                        {activityAt && <time className="block" dateTime={activityAt}>{formatDateTime(activityAt)}</time>}
                      </span>
                    )}
                  </span>
                  {s.pinned_at && <Pin className="size-3 shrink-0" aria-label={t('chat.list.pinned', 'Pinned')} />}
                  <ChatRunStatus chatId={s.chat_id} run={s.latest_run} />
                </DropdownMenuItem>
                <DropdownMenuSub>
                  <DropdownMenuSubTrigger className="shrink-0 px-2 [&>svg:last-child]:hidden"
                    aria-label={t('nav.chatHistory.actions', 'Actions for conversation {{name}}', { name: label })}>
                    <span><MoreHorizontal className="size-4" /></span>
                  </DropdownMenuSubTrigger>
                  <DropdownMenuPortal>
                    <DropdownMenuSubContent>
                      {actionsFor(s).map(({ label: actionLabel, icon: Icon, disabled, onSelect }) => (
                        <DropdownMenuItem key={actionLabel} disabled={disabled} onSelect={onSelect}>
                          <Icon className="mr-2 size-4" />{actionLabel}
                        </DropdownMenuItem>
                      ))}
                    </DropdownMenuSubContent>
                  </DropdownMenuPortal>
                </DropdownMenuSub>
              </div>
            );
          })
        ) : (
          <div className="px-2 py-1.5 text-xs text-muted-foreground">{view === 'archived' ? t('chat.list.emptyArchived', 'No archived conversations.') : t('no_chats', 'No chats yet.')}</div>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
