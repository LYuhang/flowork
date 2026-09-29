import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Building2, Check, UserRound } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';
import { toast } from 'sonner';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { listOrganizations } from '@/lib/api/organizations';
import { organizationsQueryKey } from '@/lib/api/organization-query-keys';
import { cn } from '@/lib/utils';
import { useAuthStore } from '@/stores/auth';

export function OrganizationSwitcher() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const activeOrganizationId = useAuthStore((state) => state.user?.tenant_id);
  const switchOrganization = useAuthStore((state) => state.switchOrganization);
  const organizations = useQuery({
    queryKey: organizationsQueryKey,
    queryFn: listOrganizations,
    enabled: Boolean(activeOrganizationId),
  });
  const [switchingId, setSwitchingId] = useState<string | null>(null);
  const active = organizations.data?.items.find(
    (item) => item.organization_id === activeOrganizationId,
  );

  const choose = async (organizationId: string) => {
    if (organizationId === activeOrganizationId || switchingId) return;
    setSwitchingId(organizationId);
    try {
      // Resource detail URLs are organization-scoped. Leave them before the
      // Session rotates so the new organization never boots against an old
      // Workflow/Task/Deployment id and strands the user in a 404 shell.
      navigate('/chat', { replace: true });
      await switchOrganization(organizationId);
      toast.success(t('organization.switched', 'Workspace switched'));
    } catch (reason) {
      toast.error(
        reason instanceof Error
          ? reason.message
          : t('organization.switchFailed', 'Could not switch workspace'),
      );
    } finally {
      setSwitchingId(null);
    }
  };

  if (!activeOrganizationId) return null;

  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="group flex h-11 max-w-[240px] items-center gap-2.5 rounded-xl bg-surface-sunken/80 px-2 text-sm text-foreground transition-[background-color,box-shadow,transform] duration-200 ease-out hover:bg-surface-hover/80 active:scale-[0.99] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring motion-reduce:transition-none"
          aria-label={t('organization.switcher', 'Switch workspace')}
          title={active?.name ?? t('organization.workspace', 'Workspace')}
          data-testid="organization-switcher"
        >
          <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-primary/10 text-primary transition-colors duration-200 group-hover:bg-primary/15 motion-reduce:transition-none">
            {active?.kind === 'business' ? (
              <Building2 className="size-4" />
            ) : (
              <UserRound className="size-4" />
            )}
          </span>
          <span className="min-w-0 flex-1 text-left leading-tight">
            <span className="block truncate text-[13px] font-medium">
              {active?.name ?? t('organization.workspace', 'Workspace')}
            </span>
            <span className="mt-0.5 block truncate text-xs text-muted-foreground">
              {t('organization.currentWorkspace', 'Current workspace')}
            </span>
          </span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-72 rounded-xl p-1.5">
        <DropdownMenuLabel className="px-2.5 py-2 text-xs font-medium text-muted-foreground">
          {t('organization.yourWorkspaces', 'Your workspaces')}
        </DropdownMenuLabel>
        <DropdownMenuSeparator className="mx-0" />
        {organizations.isLoading ? (
          <DropdownMenuItem disabled className="rounded-lg px-2.5 py-2.5">
            {t('common.loading', 'Loading…')}
          </DropdownMenuItem>
        ) : organizations.isError ? (
          <DropdownMenuItem disabled className="rounded-lg px-2.5 py-2.5 text-destructive">
            {t('organization.loadFailed', 'Could not load workspaces')}
          </DropdownMenuItem>
        ) : (
          organizations.data?.items.map((organization) => {
            const selected = organization.organization_id === activeOrganizationId;
            return (
              <DropdownMenuItem
                key={organization.organization_id}
                disabled={Boolean(switchingId) || organization.status !== 'active'}
                onSelect={() => void choose(organization.organization_id)}
                aria-current={selected ? 'true' : undefined}
                className={cn(
                  'items-center gap-3 rounded-lg px-2.5 py-2.5 transition-colors duration-150',
                  selected && 'bg-primary/[0.07]',
                )}
              >
                <span className={cn(
                  'grid size-8 shrink-0 place-items-center rounded-lg bg-surface-sunken text-muted-foreground',
                  selected && 'bg-primary/10 text-primary',
                )}>
                  {organization.kind === 'business' ? (
                    <Building2 className="size-4" />
                  ) : (
                    <UserRound className="size-4" />
                  )}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] font-medium">{organization.name}</span>
                  <span className="mt-0.5 block truncate text-xs text-muted-foreground">
                    {t(`organization.roles.${organization.role}`, organization.role)} ·{' '}
                    {t(`organization.kinds.${organization.kind}`, organization.kind)}
                  </span>
                </span>
                {selected ? <Check className="size-4 text-primary" /> : null}
              </DropdownMenuItem>
            );
          })
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
