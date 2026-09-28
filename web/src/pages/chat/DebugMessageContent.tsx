import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';

const PREVIEW = 1200;
const STEP = 8192;

/** Fold presentation only: database messages are never truncated or rewritten. */
export function DebugMessageContent({ content }: { content: string }) {
  const { t } = useTranslation();
  const [visible, setVisible] = useState(PREVIEW);
  return <div>
    <pre className="m-0 max-h-[60vh] overflow-auto whitespace-pre-wrap break-words px-3 py-2 font-mono text-xs leading-5">
      {content.slice(0, visible)}
    </pre>
    {(visible < content.length || visible > PREVIEW) && <div className="flex flex-wrap items-center gap-2 px-3 pb-2">
      <span className="text-xs text-muted-foreground">{t('chat.debug.contentProgress', '{{shown}} / {{total}} characters', {
        shown: Math.min(visible, content.length), total: content.length,
      })}</span>
      {visible < content.length && <Button type="button" variant="ghost" size="sm" onClick={() => setVisible(value => value + STEP)}>
        {t('chat.debug.showMore', 'Show more')}
      </Button>}
      {visible > PREVIEW && <Button type="button" variant="ghost" size="sm" onClick={() => setVisible(PREVIEW)}>
        {t('chat.debug.collapseContent', 'Collapse')}
      </Button>}
    </div>}
  </div>;
}
