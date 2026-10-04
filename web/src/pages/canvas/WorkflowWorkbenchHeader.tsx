import { ResourceAccessBadge } from '@/components/resources/ResourceAccessBadge';
import type { ResourceAccess } from '@/lib/api/organizations';
import { ArrowLeft } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';
import { EditableWorkflowName } from '@/app/EditableWorkflowName';
import { OrganizationSwitcher } from '@/components/shared/OrganizationSwitcher';
import { UserMenuDropdown } from '@/components/shared/UserMenuDropdown';
import { Button } from '@/components/ui/button';

export function WorkflowWorkbenchHeader({
  workflowId,
  readOnlyName,
  access,
}: {
  workflowId: string;
  readOnlyName: boolean;
  access?: ResourceAccess;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();

  return (
    <header className="surface-topbar flex h-[52px] shrink-0 items-center justify-between gap-3 px-3 text-ui">
      <div className="flex min-w-0 items-center gap-2">
        <Button
          type="button"
          data-testid="header-back"
          variant="quiet"
          size="sm"
          onClick={() => navigate('/workspace')}
          className="shrink-0 gap-1.5"
          aria-label={t('workflow.backToWorkflows', 'Back to workflows')}
        >
          <ArrowLeft className="h-4 w-4" />
          <span className="hidden sm:inline">{t('nav.workspace', 'Workflows')}</span>
        </Button>
        <span className="h-4 w-px bg-edge-structural" aria-hidden="true" />
        <div className="min-w-0">
          <EditableWorkflowName wfId={workflowId} readOnly={readOnlyName} />
        </div>
        {access && <ResourceAccessBadge access={access} />}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        <div className="hidden lg:block"><OrganizationSwitcher /></div>
        <UserMenuDropdown />
      </div>
    </header>
  );
}
