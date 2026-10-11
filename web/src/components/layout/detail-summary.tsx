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
  layout = 'stacked',
}: {
  items: DetailSummaryItem[];
  className?: string;
  layout?: 'stacked' | 'rows';
}) {
  return (
    <dl data-slot="detail-properties" className={cn('grid text-sm sm:grid-cols-2', layout === 'rows' ? 'gap-x-8 gap-y-0' : 'gap-x-6 gap-y-4', className)}>
      {items.map((item, index) => (
        <div key={index} className={cn('min-w-0', layout === 'rows' && 'grid grid-cols-[minmax(6rem,36%)_minmax(0,1fr)] items-baseline gap-2 border-b border-edge-subtle py-3.5 sm:grid-cols-[minmax(7rem,35%)_minmax(0,1fr)] sm:gap-3', item.wide && 'sm:col-span-2')}>
          <dt className={cn('font-medium', layout === 'rows' ? 'text-[13px] leading-5 text-content-secondary' : 'text-xs uppercase tracking-[0.06em] text-content-tertiary')}>
            {item.label}
          </dt>
          <dd className={cn('text-content-primary', layout === 'rows' ? 'min-w-0 [overflow-wrap:anywhere] text-sm font-normal leading-6' : 'mt-1 break-words font-medium')}>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}
