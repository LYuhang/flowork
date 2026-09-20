import { memo, useMemo, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';

const PAGE_SIZE = 20;
const STRING_PAGE_SIZE = 500;

/** A small, lazy tree for the canvas: no full pretty-print or recursive traversal
 * of large payloads, and no HTML interpretation of arbitrary node output. */
export const NodeJsonPreview = memo(function NodeJsonPreview({ value }: { value: unknown }) {
  return (
    <div data-node-json-preview className="min-w-0 select-text break-words font-mono text-xs leading-5 text-content-primary [overflow-wrap:anywhere]">
      <JsonEntry value={value} initialOpen />
    </div>
  );
});

function JsonEntry({ value, name, initialOpen = false }: { value: unknown; name?: string; initialOpen?: boolean }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(initialOpen);
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);
  const [visibleChars, setVisibleChars] = useState(STRING_PAGE_SIZE);
  const isArray = Array.isArray(value);
  const isObject = value !== null && typeof value === 'object';
  const keys = useMemo(() => isObject && !isArray ? Object.keys(value) : [], [value, isObject, isArray]);
  const label = name === undefined ? null : <span className="text-content-secondary">{JSON.stringify(name)}: </span>;

  if (!isObject) {
    const longString = typeof value === 'string' && value.length > visibleChars;
    const text = typeof value === 'string'
      ? JSON.stringify(value.slice(0, visibleChars))
      : String(value);
    return (
      <div data-json-leaf>
        {label}<span>{text}{longString ? '…' : ''}</span>
        {longString ? (
          <button type="button" className="ml-1 text-content-secondary underline" onClick={() => setVisibleChars((count) => count + STRING_PAGE_SIZE)}>
            {t('canvas.outputPreview.showMore', 'Show more')}
          </button>
        ) : null}
      </div>
    );
  }

  const size = isArray ? value.length : keys.length;
  const delimiters = isArray ? ['[', ']'] : ['{', '}'];
  if (size === 0) return <div>{label}{delimiters.join('')}</div>;
  const count = Math.min(size, visibleCount);
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        className="flex max-w-full items-start gap-0.5 text-left hover:text-content-secondary focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-focus"
        onClick={() => setOpen((previous) => !previous)}
      >
        <Chevron className="mt-1 h-3 w-3 shrink-0" aria-hidden />
        <span className="min-w-0">{label}{delimiters[0]}{size}{delimiters[1]}</span>
      </button>
      {open ? (
        <div className="ml-1 border-l border-edge-structural pl-2">
          {Array.from({ length: count }, (_, index) => {
            const key = isArray ? String(index) : keys[index];
            return <JsonEntry key={key} name={key} value={isArray ? value[index] : (value as Record<string, unknown>)[key]} />;
          })}
          {count < size ? (
            <button type="button" className="text-content-secondary underline" onClick={() => setVisibleCount((previous) => previous + PAGE_SIZE)}>
              {t('canvas.outputPreview.showMore', 'Show more')} ({size - count})
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
