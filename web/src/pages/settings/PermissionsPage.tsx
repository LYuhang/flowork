import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Navigate } from 'react-router';
import { listOrganizations } from '@/lib/api/organizations';
import { organizationsQueryKey } from '@/lib/api/organization-query-keys';
import { useAuthStore } from '@/stores/auth';
import { OrganizationSettingsPanel } from './OrganizationSettingsPanel';

export function PermissionsPage() {
  const { t } = useTranslation();
  const organizationId = useAuthStore((state) => state.user?.tenant_id);
  const organizations = useQuery({ queryKey: organizationsQueryKey, queryFn: listOrganizations, refetchInterval: 15000, refetchOnWindowFocus: 'always' });
  if (organizations.isPending) return <div className="p-6">{t('common.loading', 'Loading…')}</div>;
  const current = organizations.data?.items.find((item) => item.organization_id === organizationId);
  if (!current || current.kind !== 'business' || current.status !== 'active' || !['owner', 'admin'].includes(current.role)) return <Navigate to="/chat" replace />;
  return <main className="h-full overflow-y-auto p-4 sm:p-6">
    <div className="mx-auto max-w-6xl space-y-5">
      <h1 className="text-xl font-semibold">{t('nav.permissions', 'Permissions')}</h1>
      <OrganizationSettingsPanel />
    </div>
  </main>;
}
