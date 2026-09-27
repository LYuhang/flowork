import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { readVfs } from '@/lib/api/vfs';

const PREVIEW = 1200;
const STEP = 8192;

/** One message owns its expansion; the rest of the transcript stays mounted. */
export function DebugMessageContent({ workspaceId, content, contentRef, partCount = 0, totalChars, truncated }: {
  workspaceId: string;
  content: string;
  contentRef?: string;
  partCount?: number;
  totalChars?: number;
  truncated?: boolean;
}) {
  const { t } = useTranslation();
  const [loaded, setLoaded] = useState(content);
  const [parts, setParts] = useState(0);
  const [visible, setVisible] = useState(PREVIEW);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  // Current-turn outputs can grow while polling; only immutable sidecar
  // content belongs in local state. Never freeze a streaming prop at mount.
  const available = contentRef ? loaded : content;
  const total = totalChars ?? available.length;
  const more = visible < available.length || Boolean(contentRef && parts < partCount);
  const expand = async () => {
    if (busy) return;
    const next = visible + STEP;
    if (next <= available.length || !contentRef || parts >= partCount) {
      setVisible(next);
      return;
    }
    setBusy(true);
    setFailed(false);
    try {
      const response = await readVfs({ wf_id: workspaceId, path: `${contentRef}/${parts}.txt` });
      if (typeof response.content !== 'string' || response.truncated) throw new Error('Incomplete debug content');
      setLoaded(previous => parts === 0 ? response.content! : previous + response.content);
      setParts(parts + 1);
      setVisible(next);
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };
  return <div>
    <pre className="m-0 max-h-[60vh] overflow-auto whitespace-pre-wrap break-words px-3 py-2 font-mono text-xs leading-5">
      {available.slice(0, visible)}
    </pre>
    {(more || visible > PREVIEW) && <div className="flex flex-wrap items-center gap-2 px-3 pb-2">
      <span className="text-xs text-muted-foreground">{t('chat.debug.contentProgress', '{{shown}} / {{total}} characters', {
        shown: Math.min(visible, available.length), total,
      })}</span>
      {more && <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => void expand()}>
        {busy ? t('chat.debug.loadingContent', 'Loading…') : failed ? t('chat.debug.retryContent', 'Retry loading') : t('chat.debug.showMore', 'Show more')}
      </Button>}
      {visible > PREVIEW && <Button type="button" variant="ghost" size="sm" onClick={() => setVisible(PREVIEW)}>
        {t('chat.debug.collapseContent', 'Collapse')}
      </Button>}
    </div>}
    {failed && <p role="alert" className="px-3 pb-2 text-xs text-destructive">{t('chat.debug.contentLoadFailed', 'Could not load this message. Retry without changing other messages.')}</p>}
    {truncated && !contentRef && <p className="px-3 pb-2 text-xs text-muted-foreground">{t('chat.debug.legacyTruncation', 'This older snapshot did not retain the rest of this message.')}</p>}
  </div>;
}
