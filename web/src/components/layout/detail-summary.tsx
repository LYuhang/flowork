import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

export interface DetailSummaryItem {
  label: ReactNode;
  value: ReactNode;
  wide?: boolean;
}

export function DetailSummary({
  items,
  className,
}: {
  items: DetailSummaryItem[];
  className?: string;
}) {
  return (
    <dl data-slot="detail-properties" className={cn('grid gap-x-6 gap-y-4 text-sm sm:grid-cols-2', className)}>
      {items.map((item, index) => (
        <div key={index} className={cn('min-w-0', item.wide && 'sm:col-span-2')}>
          <dt className="text-xs font-medium uppercase tracking-[0.06em] text-content-tertiary">
            {item.label}
          </dt>
          <dd className="mt-1 break-words font-medium text-content-primary">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}
