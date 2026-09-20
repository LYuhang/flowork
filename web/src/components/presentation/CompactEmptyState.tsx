import { Inbox, type LucideIcon } from 'lucide-react';
import type { HTMLAttributes, ReactNode } from 'react';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

export function CompactEmptyState({
  title,
  description,
  actionLabel,
  onAction,
  icon: Icon = Inbox,
  className,
  ...props
}: {
  title: ReactNode;
  description?: ReactNode;
  actionLabel?: ReactNode;
  onAction?: () => void;
  icon?: LucideIcon;
  className?: string;
} & Omit<HTMLAttributes<HTMLElement>, 'title'>) {
  return (
    <section
      className={cn(
        'flex min-h-32 flex-wrap items-center gap-4 rounded-lg border border-dashed border-edge-structural bg-surface-sunken/45 px-5 py-4',
        className,
      )}
      {...props}
    >
      <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-surface-raised text-content-tertiary ring-1 ring-inset ring-edge-subtle">
        <Icon className="size-5" aria-hidden="true" />
      </span>
      <div className="min-w-0 flex-1 basis-40">
        <h3 className="text-sm font-medium text-content-primary">{title}</h3>
        {description ? <p className="mt-1 max-w-2xl text-sm leading-5 text-content-secondary">{description}</p> : null}
      </div>
      {actionLabel && onAction ? (
        <Button size="sm" onClick={onAction} className="shrink-0">
          {actionLabel}
        </Button>
      ) : null}
    </section>
  );
}
