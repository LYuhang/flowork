import { useTranslation } from 'react-i18next';
import type { ResourceAccess } from '@/lib/api/organizations';
import { StatusBadge } from '@/components/ui/status';

/** Describe effective actions, including combined editor/operator grants. */
export function ResourceAccessBadge({ access }: { access: ResourceAccess }) {
  const { t } = useTranslation();
  const capabilities = new Set(access.capabilities);
  const edit = capabilities.has('update');
  const execute = capabilities.has('execute');
  const label = capabilities.has('manage_access') ? 'manage'
    : edit && execute ? 'editExecute' : edit ? 'edit' : execute ? 'execute' : 'read';
  return <StatusBadge status="neutral" showDot={false}>{t(`resourceAccess.${label}`)}</StatusBadge>;
}
