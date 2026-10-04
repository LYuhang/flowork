import { useMemo } from 'react';
import { useInfiniteQuery } from '@tanstack/react-query';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';

import { ActionableError } from '@/components/presentation/ActionableError';
import { CompactEmptyState } from '@/components/presentation/CompactEmptyState';
import { ResourceIcon } from '@/components/presentation/ResourceIcon';
import { ResourceProvenanceLine } from '@/components/resources/ResourceProvenanceLine';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { StatusBadge } from '@/components/ui/status';
import {
  listSharedResources,
  type ShareableResourceKind,
} from '@/lib/api/organizations';
import type { ResourceKind } from '@/lib/presentation/resource-visuals';
import { useFormatDateTime } from '@/lib/timezone';

const PAGE_SIZE = 30;

const PRESENTATION: Record<
  ShareableResourceKind,
  { kind: ResourceKind; path: (id: string) => string }
> = {
  workflow: { kind: 'workflow', path: (id) => `/workflow/${encodeURIComponent(id)}` },
  task: { kind: 'task', path: (id) => `/tasks/${encodeURIComponent(id)}` },
  deployment: { kind: 'deployment', path: (id) => `/deployments/${encodeURIComponent(id)}` },
  skill_installation: { kind: 'skill', path: (id) => `/skills/${encodeURIComponent(id)}` },
  knowledge_base: { kind: 'knowledge', path: (id) => `/knowledge/${encodeURIComponent(id)}` },
};

export function SharedResourceList({
  resourceType,
  search = '',
}: {
  resourceType: ShareableResourceKind;
  search?: string;
}) {
  const { t } = useTranslation();
  const formatTime = useFormatDateTime();
  const query = useInfiniteQuery({
    queryKey: ['shared-resources', resourceType],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => listSharedResources(
      resourceType,
      PAGE_SIZE,
      pageParam,
    ),
    getNextPageParam: (page) => page.next_offset ?? undefined,
    retry: false,
  });
  const items = useMemo(() => {
    const all = query.data?.pages.flatMap((page) => page.items) ?? [];
    const needle = search.trim().toLocaleLowerCase();
    if (!needle) return all;
    return all.filter((item) => (
      `${item.name} ${item.description} ${item.provenance.owner.display_name}`
        .toLocaleLowerCase()
        .includes(needle)
    ));
  }, [query.data?.pages, search]);
  const visual = PRESENTATION[resourceType];

  if (query.isLoading) {
    return (
      <div className="grid gap-2" aria-label={t('resourceScope.loading', 'Loading shared resources')}>
        {Array.from({ length: 5 }, (_, index) => (
          <Skeleton key={index} className="h-20 rounded-lg" />
        ))}
      </div>
    );
  }
  if (query.isError) {
    return (
      <ActionableError
        title={t('resourceScope.loadFailed', 'Could not load shared resources')}
        description={t('resourceScope.loadFailedHint', 'Check the connection and try again.')}
        actionLabel={t('retry', 'Retry')}
        onAction={() => void query.refetch()}
        technicalDetails={query.error instanceof Error ? query.error.message : String(query.error)}
        technicalDetailsLabel={t('common.technicalDetails', 'Technical details')}
      />
    );
  }
  if (!items.length) {
    return (
      <CompactEmptyState
        title={search.trim()
          ? t('resourceScope.noMatch', 'No shared resources match this search')
          : t('resourceScope.empty', 'Nothing has been shared with you yet')}
        description={search.trim()
          ? t('resourceScope.noMatchHint', 'Try a different name or owner.')
          : t('resourceScope.emptyHint', 'Resources shared directly with your account will appear here.')}
      />
    );
  }

  return (
    <div className="min-h-0">
      <div className="divide-y divide-edge-subtle border-y border-edge-subtle">
        {items.map((item) => (
          <Link
            key={`${item.resource_type}:${item.resource_id}`}
            to={visual.path(item.resource_id)}
            className="interactive-row group flex min-h-20 items-center gap-4 px-3 py-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus"
          >
            <ResourceIcon kind={visual.kind} size="lg" className="size-10 rounded-lg" />
            <span className="min-w-0 flex-1">
              <span className="flex min-w-0 items-center gap-2">
                <span className="truncate text-sm font-medium text-content-primary">
                  {item.name}
                </span>
                {item.access.effective_role ? (
                  <StatusBadge status="neutral">
                    {t(
                      `share.role.${item.access.effective_role}`,
                      item.access.effective_role,
                    )}
                  </StatusBadge>
                ) : null}
              </span>
              {item.description ? (
                <span className="mt-0.5 block truncate text-sm text-content-secondary">
                  {item.description}
                </span>
              ) : null}
              <span className="mt-1 flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5">
                <ResourceProvenanceLine provenance={item.provenance} />
                <span className="text-xs text-content-tertiary" aria-hidden="true">·</span>
                <span className="text-xs text-content-tertiary">
                  {t('resourceScope.updatedAt', 'Updated {{time}}', {
                    time: formatTime(item.updated_at),
                  })}
                </span>
              </span>
            </span>
          </Link>
        ))}
      </div>
      {query.hasNextPage ? (
        <div className="flex justify-center pt-4">
          <Button
            variant="outline"
            disabled={query.isFetchingNextPage}
            onClick={() => void query.fetchNextPage()}
          >
            {query.isFetchingNextPage
              ? t('resourceScope.loadingMore', 'Loading…')
              : t('resourceScope.loadMore', 'Load more')}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
