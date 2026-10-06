import { useState } from 'react';
import { useTranslation } from 'react-i18next';

/** Mount message bodies only on demand; page long runs to bound DOM work. */
export function ExecutionTraces({ messages }: { messages: unknown[] }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [count, setCount] = useState(20);
  const [previousMessages, setPreviousMessages] = useState(messages);
  if (previousMessages !== messages) {
    setPreviousMessages(messages);
    setCount(20);
    setOpen(false);
  }
  return <details open={open} className="rounded-md border p-2 text-xs" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary className="cursor-pointer font-medium">
      {t('inspector.node.execution_traces', 'Execution traces')} <code>__traces__</code> · {messages.length}
    </summary>
    {open && <div className="mt-2 max-h-96 space-y-2 overflow-auto">
      {messages.slice(0, count).map((message, index) => <TraceEntry key={index} message={message} index={index} />)}
      {count < messages.length && <button type="button" className="underline" onClick={() => setCount(value => value + 20)}>
        {t('common.loadMore', 'Load more')}
      </button>}
    </div>}
  </details>;
}

function TraceEntry({ message, index }: { message: unknown; index: number }) {
  const [open, setOpen] = useState(false);
  const role = message && typeof message === 'object' && 'role' in message ? String(message.role) : 'message';
  return <details className="rounded bg-muted p-2" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary className="cursor-pointer font-mono">{index + 1}. {role}</summary>
    {open && <TraceMessage message={message} />}
  </details>;
}

function TraceMessage({ message }: { message: unknown }) {
  // The browser keeps closed details visually collapsed. Limit each body until
  // explicitly expanded so a single huge tool result cannot dominate the DOM.
  const [expanded, setExpanded] = useState(false);
  const { t } = useTranslation();
  const text = JSON.stringify(message, null, 2) ?? '';
  return <>
    <pre className="mt-2 whitespace-pre-wrap break-all select-text">{expanded ? text : text.slice(0, 8000)}</pre>
    {!expanded && text.length > 8000 && <button type="button" className="underline" onClick={() => setExpanded(true)}>{t('common.showMore', 'Show more')}</button>}
  </>;
}
