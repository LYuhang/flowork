import { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronUp } from 'lucide-react';
import { ContextAttachmentCard } from './ContextAttachmentCard';
import { contextAttachmentKey, type ChatAttachment } from './chat-attachments';

const COLLAPSED_COUNT = 4;

/** History mounts only the visible cards; opening one card loads its details. */
export function ContextAttachmentList({ attachments }: { attachments: readonly ChatAttachment[] }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const id = useId();
  if (!attachments.length) return null;
  const visible = expanded ? attachments : attachments.slice(0, COLLAPSED_COUNT);
  return <div className="mt-2 max-w-full" data-role="message-attachments">
    <div id={id} className="flex max-w-full flex-wrap gap-1.5">
      {visible.map(attachment => <ContextAttachmentCard key={contextAttachmentKey(attachment)} attachment={attachment} />)}
    </div>
    {attachments.length > COLLAPSED_COUNT && <button type="button"
      className="mt-2 inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-surface-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
      aria-expanded={expanded} aria-controls={id} onClick={() => setExpanded(value => !value)}>
      {expanded ? <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" /> : <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />}
      {expanded ? t('composer.context.collapseAttachments', 'Collapse attachments')
        : t('composer.context.moreAttachments', { defaultValue: 'Show {{count}} more attachments', count: attachments.length - COLLAPSED_COUNT })}
    </button>}
  </div>;
}
