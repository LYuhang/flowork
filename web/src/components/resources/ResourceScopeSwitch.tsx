import { useQuery } from '@tanstack/react-query';
import { Building2, Inbox, UserRound } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { listOrganizations } from '@/lib/api/organizations';
import { organizationsQueryKey } from '@/lib/api/organization-query-keys';
import { cn } from '@/lib/utils';
import { useAuthStore } from '@/stores/auth';

export type ResourceListScope = 'owned' | 'shared';

export function ResourceScopeSwitch({
  value,
  onValueChange,
}: {
  value: ResourceListScope;
  onValueChange: (value: ResourceListScope) => void;
}) {
  const { t } = useTranslation();
  const activeOrganizationId = useAuthStore((state) => state.user?.tenant_id ?? '');
  const organizations = useQuery({
    queryKey: organizationsQueryKey,
    queryFn: listOrganizations,
    enabled: Boolean(activeOrganizationId),
  });
  const active = organizations.data?.items.find(
    (item) => item.organization_id === activeOrganizationId,
  );
  const isBusiness = active?.kind === 'business';
  const OwnerIcon = isBusiness ? Building2 : UserRound;

  return (
    <div
      className="inline-flex w-fit items-center rounded-lg border border-edge-subtle bg-surface-sunken/60 p-1"
      role="group"
      aria-label={t('resourceScope.label', 'Resource scope')}
    >
      <button
        type="button"
        className={cn(
          'inline-flex h-8 items-center gap-2 rounded-md px-3 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus',
          value === 'owned'
            ? 'bg-background text-foreground shadow-sm'
            : 'text-muted-foreground hover:text-foreground',
        )}
        aria-pressed={value === 'owned'}
        title={isBusiness ? t('resourceScope.organizationHint', 'Resources in this company that you can access. Creating a resource does not share it with everyone.') : active?.name}
        onClick={() => onValueChange('owned')}
      >
        <OwnerIcon className="size-3.5" aria-hidden="true" />
        {isBusiness
          ? t('resourceScope.organization', 'Organization')
          : t('resourceScope.mine', 'My resources')}
      </button>
      <button
        type="button"
        className={cn(
          'inline-flex h-8 items-center gap-2 rounded-md px-3 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus',
          value === 'shared'
            ? 'bg-background text-foreground shadow-sm'
            : 'text-muted-foreground hover:text-foreground',
        )}
        aria-pressed={value === 'shared'}
        onClick={() => onValueChange('shared')}
      >
        <Inbox className="size-3.5" aria-hidden="true" />
        {t('resourceScope.shared', 'Shared with me')}
      </button>
    </div>
  );
}
