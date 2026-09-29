import type { ComponentType, ReactNode } from 'react';
import { ArrowLeft } from 'lucide-react';
import { Link } from 'react-router';
import { PageHeader } from '@/components/layout/page-header';
import type { ResourceKind } from '@/lib/presentation/resource-visuals';
import { cn } from '@/lib/utils';

export function EntityDetailShell({
  backTo,
  backLabel,
  title,
  description,
  icon: Icon,
  resourceKind,
  status,
  actions,
  metadata,
  children,
  className,
}: {
  backTo: string;
  backLabel: string;
  title: ReactNode;
  description?: ReactNode;
  icon?: ComponentType<{ className?: string; 'aria-hidden'?: boolean }>;
  resourceKind?: ResourceKind;
  status?: ReactNode;
  actions?: ReactNode;
  metadata?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className="page-shell page-shell-contained" data-page-archetype="continuous-detail">
      <div className={cn('page-content page-scroll-region w-full max-w-6xl', className)}>
        <header
          className="shrink-0 border-b border-edge-subtle pb-4"
          data-page-region="header"
        >
          <Link
            to={backTo}
            className="mb-3 inline-flex min-h-8 items-center gap-1.5 rounded-md px-1.5 text-sm text-content-secondary hover:bg-surface-hover hover:text-content-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          >
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
            {backLabel}
          </Link>
          <PageHeader
            wrapTitle
            title={title}
            description={description}
            icon={Icon}
            resourceKind={resourceKind}
            status={status}
            actions={actions}
            metadata={metadata}
          />
        </header>
        <div className="contents" data-page-region="detail-content">
          {children}
        </div>
      </div>
    </div>
  );
}
