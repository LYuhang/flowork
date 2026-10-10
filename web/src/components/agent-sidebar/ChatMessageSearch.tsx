import { useEffect, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, Search, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { searchChatMessages, type MessageSearchMatch } from '@/lib/api/chat-engagement';

export function ChatMessageSearch({ chatId, onLocate }: {
  chatId: string;
  onLocate: (id: string | null) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [matches, setMatches] = useState<MessageSearchMatch[]>([]);
  const [index, setIndex] = useState(0);
  const [scanned, setScanned] = useState(0);
  const [state, setState] = useState<'idle' | 'searching' | 'complete' | 'cancelled' | 'error'>('idle');
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);

  const search = async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setMatches([]);
    setIndex(0);
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
  const locate = (next: number) => {
    setIndex(next);
    onLocate(matches[next].id);
  };
  const match = matches[index];
  // API offsets count Unicode code points, not UTF-16 code units.
  const excerpt = match ? Array.from(match.excerpt) : [];
  return <div className="flex-none border-b px-3 py-1.5" data-role="chat-message-search">
    {!open ? <Button variant="ghost" size="sm" onClick={() => setOpen(true)}>
      <Search className="h-3.5 w-3.5" />{t('chat.search.open')}
    </Button> : <>
      <form className="flex items-center gap-1" onSubmit={event => { event.preventDefault(); if (query.trim()) void search(); }}>
        <Input autoFocus aria-label={t('chat.search.query')} value={query} maxLength={200}
          onChange={event => setQuery(event.target.value)} className="min-w-0 flex-1" />
        <Button type="submit" size="icon" variant="ghost" disabled={!query.trim() || state === 'searching'} aria-label={t('chat.search.submit')}><Search className="h-4 w-4" /></Button>
        <Button type="button" size="icon" variant="ghost" aria-label={t('chat.search.close')} onClick={() => {
          request.current?.abort(); setOpen(false); setState('idle'); setMatches([]); onLocate(null);
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
      {match && <div className="mt-1 flex items-center gap-1">
        <Button size="icon" variant="ghost" disabled={index === 0} aria-label={t('chat.search.previous')} onClick={() => locate(index - 1)}><ArrowUp className="h-4 w-4" /></Button>
        <span className="text-xs tabular-nums">{index + 1}/{matches.length}</span>
        <Button size="icon" variant="ghost" disabled={index === matches.length - 1} aria-label={t('chat.search.next')} onClick={() => locate(index + 1)}><ArrowDown className="h-4 w-4" /></Button>
        <button type="button" className="min-w-0 flex-1 rounded px-1 text-left text-xs hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" onClick={() => locate(index)} aria-label={t('chat.search.locate')}>
          <span className="line-clamp-2 break-words">{excerpt.slice(0, match.match_start).join('')}<mark className="bg-primary/20 text-foreground">{excerpt.slice(match.match_start, match.match_end).join('')}</mark>{excerpt.slice(match.match_end).join('')}</span>
        </button>
      </div>}
    </>}
  </div>;
}
