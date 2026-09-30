import { useEffect } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AsyncState } from '@/components/ui/async-state';
import { fetchChatWorkspace } from '@/lib/api/queries/chats';
import { useUIStore } from '@/stores/ui';

/** Authorize deep links before changing the user's current conversation. */
export function OpenReferencedChatPage() {
  const { chatId } = useParams();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const focusJob = searchParams.get('focusJob')?.slice(0, 512);
  const focusMessage = searchParams.get('focusMessage')?.slice(0, 512);
  const queryClient = useQueryClient();
  const { t } = useTranslation();
  const query = useQuery({ queryKey: ['open-referenced-chat', chatId],
    queryFn: () => fetchChatWorkspace(chatId!), enabled: !!chatId, staleTime: 0, gcTime: 0, retry: false });
  useEffect(() => {
    if (!query.data || query.isFetching || query.isError || !chatId) return;
    const state = useUIStore.getState();
    state.setActiveProjectId(query.data.project_id ?? null);
    state.setActiveChatId('chat', chatId);
    state.setChatEntryIntent('select');
    queryClient.setQueryData(['chat-workspace', chatId], query.data);
    const next = new URLSearchParams({ resumeChat: chatId });
    if (focusMessage) next.set('focusMessage', focusMessage);
    if (focusJob) next.set('focusJob', focusJob);
    navigate(`/chat?${next}`, { replace: true });
  }, [chatId, focusMessage, focusJob, navigate, queryClient, query.data, query.isError, query.isFetching]);
  return <AsyncState kind={query.isError || !chatId ? 'error' : 'loading'}
    title={query.isError || !chatId ? t('preview.reference.unavailable', 'The source conversation is unavailable or you no longer have access.')
      : t('preview.reference.checking', 'Checking the source conversation…')} />;
}
