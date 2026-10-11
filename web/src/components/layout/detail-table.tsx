import type { ComponentProps } from 'react';
import { cn } from '@/lib/utils';
import './detail-table.css';

/** Shared tabular density for execution records and deployment metrics. */
export function DetailTable({ className, ...props }: ComponentProps<'table'>) {
  return <table {...props} className={cn('detail-table w-full', className)} />;
}
