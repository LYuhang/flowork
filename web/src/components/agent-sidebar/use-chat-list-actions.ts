import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Pin, PinOff } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { updateChatListState, type ChatListStateChange } from '@/lib/api/chat-engagement';
import type { ChatListItem, ChatSessionsPage } from '@/lib/api/queries/chats';

type ListItem = Pick<ChatListItem, 'chat_id' | 'pinned_at'>;

/** Metadata-only actions, shared by the main navigation and history menus. */
export function useChatListActions() {
  const { t } = useTranslation();
  const client = useQueryClient();
  const mutation = useMutation({
    mutationFn: ({ chatId, change }: { chatId: string; change: ChatListStateChange }) => updateChatListState(chatId, change),
    retry: false,
    onSuccess: (state, { chatId }) => {
      client.setQueriesData<ChatSessionsPage>({ queryKey: ['chats'] }, old => old ? {
        ...old, items: old.items.map(item => item.chat_id === chatId ? { ...item, ...state } : item),
      } : old);
      void client.invalidateQueries({ queryKey: ['chats'] });
    },
    onError: () => toast.error(t('chat.list.updateFailed', 'Could not update the conversation list. Try again.')),
  });
  return (item: ListItem) => [
    {
      label: item.pinned_at ? t('chat.list.unpin', 'Unpin') : t('chat.list.pin', 'Pin'),
      icon: item.pinned_at ? PinOff : Pin,
      disabled: mutation.isPending,
      onSelect: () => mutation.mutate({ chatId: item.chat_id, change: { pinned: !item.pinned_at } }),
    },
  ];
}
