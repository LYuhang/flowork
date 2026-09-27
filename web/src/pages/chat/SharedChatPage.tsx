import { useEffect } from 'react';
import { useParams } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Share2 } from 'lucide-react';
import { fetchPublicShare } from '@/lib/api/chat-engagement';
import { Markdown } from '@/components/agent-sidebar/Markdown';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';

export function SharedChatPage() {
  const { token = '' } = useParams();
  const { t } = useTranslation();
  const share = useQuery({ queryKey: ['public-chat-share', token], queryFn: () => fetchPublicShare(token),
    retry: false, gcTime: 0, staleTime: 0, refetchOnWindowFocus: true, refetchInterval: 30_000 });
  useEffect(() => {
    const meta = document.createElement('meta');
    meta.name = 'robots'; meta.content = 'noindex, nofollow, noarchive';
    document.head.appendChild(meta);
    const referrer = document.createElement('meta');
    referrer.name = 'referrer'; referrer.content = 'no-referrer';
    document.head.appendChild(referrer);
    return () => { meta.remove(); referrer.remove(); };
  }, []);
  return (
    <main className="min-h-screen bg-surface-work px-4 py-8 sm:px-8" data-role="shared-chat">
      <div className="mx-auto max-w-3xl">
        <header className="mb-8 border-b border-edge-structural pb-5">
          <div className="mb-3 flex items-center gap-2 text-sm text-muted-foreground"><Share2 className="h-4 w-4" /><span translate="no">Flowork</span><span aria-hidden="true">·</span>{t('chat.share.readOnly', 'Read-only snapshot')}</div>
          <h1 className="break-words text-xl font-semibold">{!share.isError && share.data?.kind === 'message' ? t('chat.share.response', 'Single response') : (!share.isError && share.data?.title) || t('chat.share.sharedConversation', 'Shared conversation')}</h1>
        </header>
        {share.isPending ? <div className="space-y-4" aria-label={t('common.loading', 'Loading')}><Skeleton className="h-20 w-4/5" /><Skeleton className="h-40 w-full" /></div>
          : share.isError ? <div role="alert" className="space-y-3 py-8">
            <p>{share.error.message === 'unavailable' ? t('chat.share.unavailable', 'This link has been revoked, expired, or is no longer available.') : t('chat.share.loadFailed', 'Could not load this conversation.')}</p>
            {share.error.message !== 'unavailable' && <Button variant="outline" onClick={() => void share.refetch()}>{t('common.retry', 'Retry')}</Button>}
          </div> : <div className="space-y-7">
            {share.data.messages.map((message) => <article key={message.id} id={`message-${message.id}`} className="min-w-0">
              <p className="mb-2 text-sm font-semibold text-muted-foreground">{message.role === 'assistant' ? 'Assistant' : t('chat.share.user', 'User')}</p>
              <Markdown publicView>{message.content}</Markdown>
            </article>)}
          </div>}
        <footer className="mt-10 border-t border-edge-subtle pt-4 text-xs text-muted-foreground">{t('chat.share.footer', 'Shared by a Flowork user. AI responses may contain mistakes.')}</footer>
      </div>
    </main>
  );
}
