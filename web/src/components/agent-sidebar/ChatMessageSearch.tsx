import { useEffect, useRef, useState } from 'react';
import { ArrowRight, Search, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { searchChatMessages, type MessageSearchMatch } from '@/lib/api/chat-engagement';

export function ChatMessageSearch({ chatId, onLocate, open: controlledOpen, onOpenChange }: {
  chatId: string;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  onLocate: (id: string | null) => void;
}) {
  const { t } = useTranslation();
  const [localOpen, setLocalOpen] = useState(false);
  const open = controlledOpen ?? localOpen;
  const setOpen = onOpenChange ?? setLocalOpen;
  const [query, setQuery] = useState('');
  const [matches, setMatches] = useState<MessageSearchMatch[]>([]);
  const [composing, setComposing] = useState(false);
  const [scanned, setScanned] = useState(0);
  const [state, setState] = useState<'idle' | 'searching' | 'complete' | 'cancelled' | 'error'>('idle');
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);

  const search = async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setMatches([]);
    setScanned(0);
    setState('searching');
    onLocate(null);
    let cursor = 0;
    try {
      do {
        const page = await searchChatMessages(chatId, query.trim(), cursor, controller.signal);
        if (controller.signal.aborted) return;
        setMatches(previous => [...previous, ...page.items]);
        setScanned(previous => previous + page.scanned);
        if (page.next_cursor === null) break;
        cursor = page.next_cursor;
      } while (!controller.signal.aborted);
      if (!controller.signal.aborted) setState('complete');
    } catch {
      if (!controller.signal.aborted) setState('error');
    }
  };

  const updateQuery = (value: string) => {
    request.current?.abort();
    setQuery(value);
    setMatches([]);
    setScanned(0);
    setState('idle');
    onLocate(null);
  };
  if (controlledOpen === false) return null;
  return <div className="flex-none border-b px-3 py-1.5" data-role="chat-message-search">
    {!open ? <Button variant="ghost" size="sm" onClick={() => setOpen(true)}>
      <Search className="h-3.5 w-3.5" />{t('chat.search.open')}
    </Button> : <>
      <form className="flex items-center gap-1" onSubmit={event => {
        event.preventDefault();
        if (query.trim() && !composing && state !== 'searching') void search();
      }}>
        <Input autoFocus aria-label={t('chat.search.query')} value={query} maxLength={200}
          onChange={event => updateQuery(event.target.value)}
          onCompositionStart={() => { request.current?.abort(); setComposing(true); }}
          onCompositionEnd={() => setComposing(false)} className="min-w-0 flex-1" />
        <Button type="submit" size="icon" variant="ghost" disabled={!query.trim() || composing || state === 'searching'} aria-label={t('chat.search.submit')}><Search className="size-4" /></Button>
        <Button type="button" size="icon" variant="ghost" aria-label={t('chat.search.close')} onClick={() => {
          request.current?.abort(); setOpen(false); updateQuery('');
        }}><X className="h-4 w-4" /></Button>
      </form>
      <p className="mt-1 text-xs text-muted-foreground">{t('chat.search.scope')}</p>
      <div role="status" className="mt-1 text-xs text-muted-foreground">
        {state === 'searching' && t('chat.search.progress', { count: scanned })}
        {state === 'complete' && t('chat.search.complete', { count: matches.length })}
        {state === 'cancelled' && t('chat.search.cancelled')}
        {state === 'error' && t('chat.search.error')}
      </div>
      {state === 'searching' && <Button size="sm" variant="ghost" onClick={() => { request.current?.abort(); setState('cancelled'); }}>{t('chat.search.cancel')}</Button>}
      {matches.length > 0 && <ul className="mt-2 max-h-[min(18rem,40vh)] overflow-y-auto rounded-md border border-edge-subtle divide-y divide-edge-subtle">
        {matches.map(match => {
          // API offsets count Unicode code points, not UTF-16 code units.
          const excerpt = Array.from(match.excerpt);
          return <li key={match.id}>
            <button type="button" className="flex min-h-11 w-full items-center gap-3 px-3 py-2 text-left text-sm hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
              onClick={() => onLocate(match.id)} aria-label={t('chat.search.locate')}>
              <span className="min-w-0 flex-1 break-words line-clamp-3">{excerpt.slice(0, match.match_start).join('')}<mark className="bg-state-danger/10 font-semibold text-state-danger">{excerpt.slice(match.match_start, match.match_end).join('')}</mark>{excerpt.slice(match.match_end).join('')}</span>
              <ArrowRight className="size-4 shrink-0 text-content-secondary" aria-hidden="true" />
            </button>
          </li>;
        })}
      </ul>}
    </>}
  </div>;
}
