import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useUIStore } from '@/stores/ui';
import { searchResourcePage, type ResourceSearchItem, type ResourceSearchKind } from '@/lib/api/resource-search';

const KINDS: ResourceSearchKind[] = ['projects', 'chats', 'tasks', 'deployments'];
interface Results {
  query: string;
  items: ResourceSearchItem[];
  cursor: string | null;
  scanned: number;
  complete: boolean;
}

export function ResourceSearch({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const [kind, setKind] = useState<ResourceSearchKind>('chats');
  return <div className="space-y-3 p-4">
    <label className="flex items-center gap-3 text-sm">
      {t('commandPalette.resources.category')}
      <select className="h-9 rounded-md border border-input bg-background px-2 text-sm" value={kind}
        onChange={event => setKind(event.target.value as ResourceSearchKind)}>
        {KINDS.map(value => <option key={value} value={value}>{t(`commandPalette.resources.${value}`)}</option>)}
      </select>
    </label>
    <p className="text-xs text-muted-foreground">{t('commandPalette.resources.scope')}</p>
    <ResourceResults key={kind} kind={kind} onClose={onClose} />
  </div>;
}

function ResourceResults({ kind, onClose }: { kind: ResourceSearchKind; onClose: () => void }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<Results | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stopped, setStopped] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);

  const search = async (append = false) => {
    const base = append && results ? results : {
      query: query.trim(), items: [], cursor: null, scanned: 0, complete: false,
    };
    if (!base.query) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setResults(base);
    setBusy(true);
    setError(null);
    setStopped(false);
    try {
      const page = await searchResourcePage(kind, base.query, base.cursor, controller.signal);
      if (controller.signal.aborted) return;
      const merged = new Map(base.items.map(item => [item.id, item]));
      for (const item of page.items) merged.set(item.id, item);
      setResults({ query: base.query, items: [...merged.values()], cursor: page.next_cursor,
        scanned: base.scanned + page.scanned, complete: page.next_cursor === null });
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : t('error'));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };
  const stop = () => {
    request.current?.abort();
    setBusy(false);
    setStopped(true);
  };
  const open = (item: ResourceSearchItem) => {
    if (kind === 'projects') {
      const ui = useUIStore.getState();
      ui.setActiveProjectId(item.id);
      ui.setActiveChatId('chat', null);
      ui.setChatEntryIntent('default');
    }
    onClose();
    navigate(kind === 'projects' ? '/chat' : kind === 'chats'
      ? `/chat/open/${encodeURIComponent(item.id)}` : `/${kind}/${encodeURIComponent(item.id)}`);
  };

  return <div className="space-y-3">
    <form className="flex gap-2" onSubmit={event => { event.preventDefault(); void search(); }}>
      <Input className="min-w-0" value={query} maxLength={200} aria-label={t('commandPalette.resources.query')}
        placeholder={t('commandPalette.resources.query')} onChange={event => setQuery(event.target.value)} />
      <Button type="submit" disabled={busy || !query.trim()}>{t('commandPalette.resources.submit')}</Button>
      {busy && <Button type="button" variant="outline" onClick={stop}>{t('commandPalette.resources.stop')}</Button>}
    </form>
    <div role="status" aria-live="polite" className="space-y-1 text-xs text-muted-foreground">
      {results && <p>{t('commandPalette.resources.progress', { query: results.query, count: results.items.length, scanned: results.scanned })}</p>}
      {busy ? <p>{t('commandPalette.resources.loading')}</p>
        : results && <p>{t(results.complete ? 'commandPalette.resources.complete' : 'commandPalette.resources.incomplete')}</p>}
      {stopped && <p>{t('commandPalette.resources.stopped')}</p>}
    </div>
    {error && <p role="alert" className="text-sm text-destructive">{t('commandPalette.resources.failed')} {error}</p>}
    {!!results?.items.length && <ul className="max-h-[40dvh] space-y-1 overflow-y-auto" aria-label={t(`commandPalette.resources.${kind}`)}>
      {results.items.map(item => <li key={item.id}>
        <Button variant="ghost" className="h-auto w-full flex-col items-start gap-1 whitespace-normal px-3 py-2 text-left" onClick={() => open(item)}>
          <span className="break-words [overflow-wrap:anywhere]">{item.title}</span>
          <span className="max-w-full truncate font-mono text-xs text-muted-foreground">{item.id}</span>
        </Button>
      </li>)}
    </ul>}
    {results && !results.complete && !busy && <Button variant="outline" onClick={() => void search(true)}>
      {t(error ? 'commandPalette.resources.retry' : 'commandPalette.resources.more')}
    </Button>}
  </div>;
}
