import type { ComponentType, ReactNode } from 'react';

import { ResourceIcon } from '@/components/presentation/ResourceIcon';
import type { ResourceKind } from '@/lib/presentation/resource-visuals';
import { cn } from '@/lib/utils';

export function PageHeader({
  title,
  description,
  icon: Icon,
  resourceKind,
  status,
  actions,
  metadata,
  eyebrow,
  className,
  wrapTitle = false,
}: {
  title: ReactNode;
  description?: ReactNode;
  icon?: ComponentType<{ className?: string; 'aria-hidden'?: boolean }>;
  resourceKind?: ResourceKind;
  status?: ReactNode;
  actions?: ReactNode;
  metadata?: ReactNode;
  eyebrow?: ReactNode;
  className?: string;
  wrapTitle?: boolean;
}) {
  return (
    <div
      className={cn('flex flex-col items-stretch gap-4 sm:flex-row sm:items-start sm:justify-between', className)}
      data-page-region="identity"
    >
      <div className="min-w-0 flex-1">
        {eyebrow ? (
          <div className="mb-1 text-xs font-medium uppercase tracking-[0.1em] text-content-tertiary">
            {eyebrow}
          </div>
        ) : null}
        <div className={cn('flex min-w-0 items-center gap-2.5', wrapTitle && 'flex-wrap')}>
          {resourceKind ? <ResourceIcon kind={resourceKind} size="md" /> : null}
          {!resourceKind && Icon ? (
            <Icon className="size-5 shrink-0 text-content-secondary" aria-hidden />
          ) : null}
          <h1 className={`text-title min-w-0 text-content-primary ${wrapTitle ? 'flex-1 break-words [overflow-wrap:anywhere]' : 'truncate'}`}>{title}</h1>
          {status}
        </div>
        {description ? (
          <p className="mt-1 max-w-3xl text-sm leading-5 text-content-secondary">{description}</p>
        ) : null}
        {metadata ? (
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-content-tertiary">
            {metadata}
          </div>
        ) : null}
      </div>
      {actions ? (
        <div
          className="flex w-full shrink-0 flex-wrap items-center gap-2 sm:w-auto sm:justify-end"
          data-page-region="primary-actions"
        >
          {actions}
        </div>
      ) : null}
    </div>
  );
}
