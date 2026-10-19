import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { resolveServicePreview } from '@/lib/api/previews';
import { usePreviewOrigin } from '@/lib/preview/context-origin';
import { useChatRenderIdentity } from '@/components/agent-sidebar/chat-render-context';
import { RefreshCw } from 'lucide-react';

export function ServicePreviewRenderer({ url, title }: { url: string; title: string }) {
  const { t } = useTranslation();
  const origin = usePreviewOrigin();
  const identity = useChatRenderIdentity();
  const chatId = origin?.chatId ?? identity?.chatId;
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{ url?: string; error?: string }>({});
  useEffect(() => {
    const abort = new AbortController();
    setState({});
    if (!chatId) {
      setState({ error: t('preview.service.noChat', 'Open this service from its original conversation.') });
      return;
    }
    void resolveServicePreview(chatId, url, abort.signal).then(
      (value) => { if (!abort.signal.aborted) setState({ url: value.url }); },
      (error: unknown) => {
        if (abort.signal.aborted) return;
        const detail = error instanceof Error ? error.message : String(error);
        setState({ error: detail.includes('domain_not_configured')
          ? t('preview.service.notConfigured', 'Service preview domain is not configured yet.')
          : detail.includes('stopped') || detail.includes('changed')
            ? t('preview.service.stopped', 'The sandbox service has stopped. Ask the Agent to check or start it.')
            : detail });
      },
    );
    return () => abort.abort();
  }, [chatId, url, attempt, t]);
  if (state.url) return <div className="flex h-full min-h-64 flex-col">
    <div className="flex items-center gap-2 border-b px-3 py-2">
      <span className="min-w-0 flex-1 truncate text-xs" title={url}>{url}</span>
      <Button size="icon" variant="ghost" aria-label={t('preview.service.reload', 'Reconnect preview')} onClick={() => setAttempt((value) => value + 1)}><RefreshCw className="size-4" /></Button>
    </div>
    <iframe className="min-h-0 w-full flex-1 border-0" src={state.url} title={title}
      sandbox="allow-scripts allow-same-origin allow-forms allow-downloads" referrerPolicy="no-referrer" />
  </div>;
  return <div className="flex h-full min-h-40 flex-col items-center justify-center gap-3 p-4 text-sm" role="status">
    <p>{state.error ?? t('preview.service.connecting', 'Connecting to the sandbox service…')}</p>
    {state.error && <Button size="sm" variant="outline" onClick={() => setAttempt((value) => value + 1)}>{t('common.retry', 'Retry')}</Button>}
  </div>;
}
