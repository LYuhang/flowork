import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { useChatListActions } from './use-chat-list-actions';

export function ArchivedChatNotice({ chat }: { chat?: {
  chat_id: string; pinned_at?: string | null; archived_at?: string | null;
} | null }) {
  const { t } = useTranslation();
  const actionsFor = useChatListActions();
  if (!chat?.archived_at) return null;
  const restore = actionsFor(chat)[1];
  return <div role="status" className="flex shrink-0 items-center justify-between gap-2 border-b border-edge-subtle bg-surface-raised px-3 py-1 text-xs text-muted-foreground">
    <span>{t('chat.list.archivedNotice', 'This conversation is archived.')}</span>
    <Button size="sm" variant="ghost" className="h-7 text-xs" disabled={restore.disabled} onClick={restore.onSelect}>
      {restore.label}
    </Button>
  </div>;
}
