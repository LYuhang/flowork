import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { resolveServicePreview, stopPreviewService, type PreviewService } from '@/lib/api/previews';
import { usePreviewOrigin } from '@/lib/preview/context-origin';
import { useChatRenderIdentity } from '@/components/agent-sidebar/chat-render-context';
import { RefreshCw, Square } from 'lucide-react';

export function ServicePreviewRenderer({ url, title }: { url: string; title: string }) {
  const { t } = useTranslation();
  const origin = usePreviewOrigin();
  const identity = useChatRenderIdentity();
  const chatId = origin?.chatId ?? identity?.chatId;
  const [attempt, setAttempt] = useState(0);
  const requestKey = JSON.stringify([chatId, url, attempt]);
  const [result, setState] = useState<{ key?: string; url?: string; error?: string; service?: PreviewService; stopped?: boolean; stopError?: string }>({});
  const [stopping, setStopping] = useState(false);
  const state = result.key === requestKey ? result : {};
  useEffect(() => {
    const abort = new AbortController();
    if (!chatId) return;
    void resolveServicePreview(chatId, url, abort.signal).then(
      (value) => { if (!abort.signal.aborted) setState({ key: requestKey, url: value.url, service: value.service }); },
      (error: unknown) => {
        if (abort.signal.aborted) return;
        const detail = error instanceof Error ? error.message : String(error);
        setState({ key: requestKey, error: detail.includes('domain_not_configured')
          ? t('preview.service.notConfigured', 'Service preview domain is not configured yet.')
          : detail.includes('stopped') || detail.includes('changed')
            ? t('preview.service.stopped', 'The sandbox service has stopped. Ask the Agent to check or start it.')
            : detail });
      },
    );
    return () => abort.abort();
  }, [chatId, url, requestKey, t]);
  async function stopService() {
    if (!chatId || !state.service || stopping) return;
    setStopping(true);
    setState(current => current.key === requestKey ? { ...current, stopError: undefined } : current);
    try {
      await stopPreviewService(chatId, url, state.service);
      setState(current => current.key === requestKey ? { key: requestKey, stopped: true } : current);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      setState(current => current.key === requestKey ? { ...current, stopError: detail } : current);
    } finally {
      setStopping(false);
    }
  }
  if (state.url) return <div className="flex h-full min-h-64 flex-col">
    <div className="flex items-center gap-2 border-b px-3 py-2">
      <span className="min-w-0 flex-1 truncate text-xs" title={url}>{url}</span>
      <Button size="icon" variant="ghost" className="text-state-danger hover:text-state-danger" disabled={stopping || !state.service?.expected.length}
        title={t('preview.service.stop', 'Stop service')} aria-label={t('preview.service.stop', 'Stop service')} onClick={() => void stopService()}><Square className="size-4 fill-current" /></Button>
      <Button size="icon" variant="ghost" aria-label={t('preview.service.reload', 'Reconnect preview')} onClick={() => setAttempt((value) => value + 1)}><RefreshCw className="size-4" /></Button>
    </div>
    {state.stopError && <p role="alert" className="px-3 py-2 text-xs text-state-danger">{state.stopError}</p>}
    <iframe className="min-h-0 w-full flex-1 border-0" src={state.url} title={title}
      sandbox="allow-scripts allow-same-origin allow-forms allow-downloads" referrerPolicy="no-referrer" />
  </div>;
  return <div className="flex h-full min-h-40 flex-col items-center justify-center gap-3 p-4 text-sm" role="status">
    <p>{state.stopped ? t('preview.service.stoppedByUser', 'Service stopped.') : !chatId ? t('preview.service.noChat', 'Open this service from its original conversation.') : state.error ?? t('preview.service.connecting', 'Connecting to the sandbox service…')}</p>
    {(state.error || state.stopped) && <Button size="sm" variant="outline" onClick={() => setAttempt((value) => value + 1)}>{state.stopped ? t('preview.service.reload', 'Reconnect preview') : t('common.retry', 'Retry')}</Button>}
  </div>;
}
