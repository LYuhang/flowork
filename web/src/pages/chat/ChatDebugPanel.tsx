import { useMemo, useState } from 'react';
import { Download, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { DebugMessageContent } from './DebugMessageContent';
import { useChatDebugTranscript } from './useChatDebugTranscript';

export function ChatDebugPanel({ chatId }: { chatId: string }) {
  const { t } = useTranslation();
  const query = useChatDebugTranscript(chatId);
  const [view, setView] = useState<'messages' | 'artifacts' | 'turns'>('messages');
  const data = query.data;
  const counts = useMemo(() => {
    const result: Record<string, number> = { user: 0, assistant: 0, tool: 0, system: 0 };
    for (const message of data?.messages ?? []) result[message.role] = (result[message.role] ?? 0) + 1;
    return result;
  }, [data?.messages]);
  const download = () => {
    if (!data) return;
    const blob = new Blob([JSON.stringify({ format: 'chatml', ...data }, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = chatId + '.chatml.json';
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return <aside className="flex h-full min-h-0 w-full min-w-0 flex-col bg-surface-work" data-role="chat-debug-panel" data-chat-id={chatId}>
    <div className="chat-pane-header flex h-11 shrink-0 items-center justify-between gap-2 px-3">
      <h2 className="truncate text-sm font-semibold">{t('chat.debug.title', 'Agent Debug')}</h2>
      <div className="flex shrink-0 gap-1">
        <Button variant="ghost" size="icon" disabled={!data || query.isFetching} onClick={download} aria-label={t('chat.debug.exportChatml', 'Download ChatML')}><Download className="h-4 w-4" /></Button>
        <Button variant="ghost" size="icon" disabled={query.isFetching} onClick={() => void query.refetch()} aria-label={t('chat.debug.refreshTranscript', 'Refresh conversation')}><RefreshCw className="h-4 w-4" /></Button>
      </div>
    </div>
    <p className="border-b px-3 py-2 text-xs text-muted-foreground">{t('chat.debug.databaseDescription', 'Complete stored conversation. Updates after sending and when the Agent finishes; not the model’s private context.')}</p>
    <div role="tablist" aria-label={t('chat.debug.views', 'Debug views')} className="chat-pane-subheader flex shrink-0 gap-3 px-3">
      {(['messages', 'artifacts', 'turns'] as const).map(tab => <button key={tab} type="button" role="tab" aria-selected={view === tab} onClick={() => setView(tab)} className={view === tab ? 'h-10 border-b-2 border-focus text-xs font-medium text-content-primary' : 'h-10 border-b-2 border-transparent text-xs font-medium text-content-tertiary'}>
        {t('chat.debug.' + tab, tab === 'messages' ? 'Messages' : tab === 'artifacts' ? 'Artifacts' : 'Turns')}{' '}{data?.[tab].length ?? 0}
      </button>)}
    </div>
    {view === 'messages' && <div className="flex shrink-0 flex-wrap gap-3 border-b px-3 py-2 text-xs text-muted-foreground">
      {Object.entries(counts).map(([role, count]) => <span key={role}>{role} {count}</span>)}
    </div>}
    <div role="tabpanel" className="min-h-0 flex-1 overflow-y-auto p-3">
      {query.isError && <p role="alert" className="mb-3 text-sm text-destructive">{t('chat.debug.databaseError', 'Could not refresh the conversation. Previously loaded messages are retained; try Refresh.')}</p>}
      {query.isFetching && <p role="status" className="mb-3 text-xs text-muted-foreground">{t('chat.debug.databaseLoading', 'Loading stored messages…')}</p>}
      {!query.isFetching && !query.isError && !data?.messages.length && <p className="text-sm text-muted-foreground">{t('chat.debug.databaseEmpty', 'No messages have been saved for this Chat yet.')}</p>}
      <div className="space-y-3">
        {view === 'messages' ? data?.messages.map(message => <article key={message.id} data-debug-message-id={message.id} data-debug-role={message.role} className="rounded-md border bg-background text-xs">
          <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
            <span className="font-medium">{message.role}</span>
            {message.meta.status != null && <span className="text-muted-foreground">{String(message.meta.status)}</span>}
            {message.visibility === 'hidden' && <span className="text-muted-foreground">{t('chat.debug.controlMessage', 'Platform control')}</span>}
            {message.turn_id && <span className="min-w-0 break-all font-mono text-muted-foreground">{message.turn_id}</span>}
          </div>
          <DebugMessageContent content={message.content} />
          {message.tool_calls.length > 0 && <details className="border-t"><summary className="cursor-pointer px-3 py-2">{t('chat.debug.toolCalls', 'Tool calls')} · {message.tool_calls.length}</summary><DebugMessageContent content={JSON.stringify(message.tool_calls, null, 2)} /></details>}
          {(message.artifact || message.attachments.length > 0) && <details className="border-t"><summary className="cursor-pointer px-3 py-2">{t('chat.debug.relatedResources', 'Artifacts and attachments')}</summary><DebugMessageContent content={JSON.stringify({ artifact: message.artifact, attachments: message.attachments }, null, 2)} /></details>}
          <details className="border-t"><summary className="cursor-pointer px-3 py-2">{t('chat.debug.messageMetadata', 'Message metadata')}</summary><DebugMessageContent content={JSON.stringify({ ...message, content: undefined, tool_calls: undefined, artifact: undefined, attachments: undefined }, null, 2)} /></details>
        </article>) : data?.[view].map(record => <section key={String(record.artifact_id ?? record.run_id)} className="rounded-md border bg-background text-xs">
          <div className="break-all border-b px-3 py-2 font-mono">{String(record.artifact_id ?? record.run_id)}</div>
          <DebugMessageContent content={JSON.stringify(record, null, 2)} />
        </section>)}
      </div>
    </div>
  </aside>;
}
