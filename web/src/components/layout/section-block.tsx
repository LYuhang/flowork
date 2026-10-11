import { useId, useState, type ReactNode } from 'react';
import { ChevronDown } from 'lucide-react';

import { cn } from '@/lib/utils';

export function SectionBlock({
  title,
  description,
  icon,
  actions,
  children,
  collapsible = false,
  defaultOpen = true,
  className,
  contentClassName,
  variant = 'card',
}: {
  title: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  collapsible?: boolean;
  defaultOpen?: boolean;
  className?: string;
  contentClassName?: string;
  variant?: 'card' | 'plain' | 'detail';
}) {
  const [open, setOpen] = useState(defaultOpen);
  const reactId = useId();
  const contentId = `section-${reactId.replaceAll(':', '')}`;

  const heading = (
    <div className={cn('flex min-w-0 flex-1 items-start gap-2.5', variant === 'detail' && !collapsible && 'basis-48')}>
      {icon ? <span className="mt-0.5 shrink-0 text-content-secondary">{icon}</span> : null}
      <div className="min-w-0">
        <h2 data-slot="section-title" className={cn('font-semibold text-content-primary', variant === 'detail' ? 'text-[15px] leading-6' : 'text-sm')}>{title}</h2>
        {description ? (
          <p data-slot="section-description" className={cn('leading-5 text-content-secondary', variant === 'detail' ? 'mt-1 text-[13px]' : 'mt-0.5 text-xs')}>{description}</p>
        ) : null}
      </div>
    </div>
  );

  return (
    <section
      data-slot="detail-section"
      className={cn(
        'shrink-0',
        variant !== 'plain'
          ? cn('overflow-hidden rounded-lg border border-edge-subtle', variant === 'detail' ? 'bg-surface-raised' : 'bg-surface-base')
          : 'border-b border-edge-subtle py-6 last:border-b-0',
        className,
      )}
    >
      <div
        data-slot="section-header"
        className={cn(
          'flex gap-3',
          variant === 'detail' ? 'flex-wrap items-center' : 'items-start',
          variant !== 'plain'
            ? 'border-b border-edge-subtle bg-surface-sunken/55 px-4 py-3'
            : 'pb-4',
        )}
      >
        {collapsible ? (
          <button
            type="button"
            className={cn('flex min-w-0 flex-1 items-start gap-3 rounded-sm text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus', variant === 'detail' && 'basis-48')}
            aria-expanded={open}
            aria-controls={contentId}
            onClick={() => setOpen((current) => !current)}
          >
            {heading}
            <ChevronDown
              className={cn('mt-0.5 size-4 shrink-0 text-content-tertiary transition-transform', !open && '-rotate-90')}
              aria-hidden="true"
            />
          </button>
        ) : heading}
        {actions ? <div className="shrink-0">{actions}</div> : null}
      </div>
      {open ? (
        <div
          id={contentId}
          className={cn(variant !== 'plain' ? 'p-4' : '', contentClassName)}
        >
          {children}
        </div>
      ) : null}
    </section>
  );
}
