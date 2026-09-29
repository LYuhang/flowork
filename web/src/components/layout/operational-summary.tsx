import type { ComponentType, ReactNode } from 'react';

import { cn } from '@/lib/utils';

type SummaryTone = 'neutral' | 'info' | 'success' | 'warning' | 'danger';

export interface OperationalSummaryItem {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
  tone?: SummaryTone;
  icon?: ComponentType<{ className?: string; 'aria-hidden'?: boolean }>;
}

const valueTone: Record<SummaryTone, string> = {
  neutral: 'text-content-primary',
  info: 'text-state-info',
  success: 'text-state-success',
  warning: 'text-state-warning',
  danger: 'text-state-danger',
};

export function OperationalSummary({
  label,
  items,
  className,
}: {
  label: string;
  items: OperationalSummaryItem[];
  className?: string;
}) {
  return (
    <section
      aria-label={label}
      className={cn(
        'relative overflow-hidden rounded-xl border border-edge-subtle bg-surface-sunken/35',
        className,
      )}
    >
      <div className="absolute inset-y-0 left-0 w-1 bg-focus/70" aria-hidden="true" />
      <dl
        className={cn(
          'grid grid-cols-2 divide-edge-subtle pl-1 sm:divide-x',
          items.length === 3 ? 'sm:grid-cols-3' : 'sm:grid-cols-2 lg:grid-cols-4',
        )}
      >
        {items.map((item, index) => {
          const tone = item.tone ?? 'neutral';
          const Icon = item.icon;
          return (
            <div key={index} className="min-w-0 px-3 py-3 sm:px-5 sm:py-4">
              <dt className="flex items-center gap-2 text-xs font-medium text-content-tertiary">
                {Icon ? <Icon className={cn('size-3.5 shrink-0', valueTone[tone])} aria-hidden /> : null}
                {item.label}
              </dt>
              <dd className="mt-1.5 min-w-0 break-words">
                <span
                  className={cn(
                    'block text-lg font-semibold leading-6 tabular-nums',
                    valueTone[tone],
                  )}
                >
                  {item.value}
                </span>
                {item.hint ? (
                  <span className="mt-1 block text-xs font-normal leading-5 text-content-secondary">
                    {item.hint}
                  </span>
                ) : null}
              </dd>
            </div>
          );
        })}
      </dl>
    </section>
  );
}
