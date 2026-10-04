import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Building2,
  CheckCircle2,
  KeyRound,
  LockKeyhole,
  Plus,
  RotateCw,
  ShieldCheck,
  Trash2,
  UserRound,
  UsersRound,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';

import { EnterpriseIdentityPanel } from '@/pages/settings/EnterpriseIdentityPanel';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { organizationsQueryKey } from '@/lib/api/organization-query-keys';
import {
  addOrganizationMember,
  createOrganizationGroup,
  getOrganizationSelf,
  listGroupMembers,
  listOrganizationGroups,
  listOrganizationMembers,
  listOrganizations,
  listServiceAccounts,
  removeGroupMember,
  rotateServiceAccountGeneration,
  setGroupMember,
  updateOrganizationMember,
  updateServiceAccountStatus,
  type OrganizationMember,
  type OrganizationRole,
  type OrganizationStatus,
  type ServiceAccount,
} from '@/lib/api/organizations';
import { useAuthStore } from '@/stores/auth';
import { cn } from '@/lib/utils';

const organizationSelfKey = (organizationId: string) =>
  ['organization-self', organizationId] as const;
const organizationMembersKey = (organizationId: string) =>
  ['organization-members', organizationId] as const;
const organizationGroupsKey = (organizationId: string) =>
  ['organization-groups', organizationId] as const;
const groupMembersKey = (organizationId: string, groupId: string) =>
  ['organization-group-members', organizationId, groupId] as const;
const serviceAccountsKey = (organizationId: string) =>
  ['organization-service-accounts', organizationId] as const;

const ORGANIZATION_ROLES: OrganizationRole[] = [
  'owner',
  'admin',
  'member',
  'guest',
  'auditor',
];
const ORGANIZATION_STATUSES: OrganizationStatus[] = [
  'invited',
  'active',
  'suspended',
  'revoking',
  'revoked',
];

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

function RoleBadge({ role }: { role: string }) {
  const { t } = useTranslation();
  return (
    <span className="inline-flex rounded-full border border-primary/15 bg-primary/[0.07] px-2.5 py-1 text-xs font-medium text-primary">
      {t(`organization.roles.${role}`, role)}
    </span>
  );
}

function StatusBadge({ status }: { status: string }) {
  const { t } = useTranslation();
  const active = status === 'active';
  return (
    <span className={cn(
      'inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium',
      active
        ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
        : 'bg-muted text-muted-foreground',
    )}>
      <span className={cn('size-1.5 rounded-full', active ? 'bg-emerald-500' : 'bg-current/50')} />
      {t(`organization.statuses.${status}`, status)}
    </span>
  );
}

function EmptyState({ children }: { children: string }) {
  return (
    <div className="rounded-xl border border-dashed border-edge-subtle bg-surface-sunken/35 px-5 py-10 text-center text-sm text-muted-foreground">
      {children}
    </div>
  );
}

function MemberIdentity({ member }: { member: OrganizationMember }) {
  return (
    <div className="flex min-w-0 items-center gap-3">
      <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-surface-sunken text-content-secondary">
        <UserRound className="size-4" />
      </span>
      <span className="min-w-0">
        <span className="block truncate text-sm font-medium">
          {member.display_name || member.email}
        </span>
        <span className="block truncate text-xs text-muted-foreground">{member.email}</span>
      </span>
    </div>
  );
}

function MemberAccessControls({
  organizationId,
  member,
  canAssignAdmin,
}: {
  organizationId: string;
  member: OrganizationMember;
  canAssignAdmin: boolean;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<{
    role: OrganizationRole;
    status: OrganizationStatus;
  } | null>(null);
  const [saved, setSaved] = useState(false);
  const role = draft?.role ?? member.role;
  const status = draft?.status ?? member.status;

  const update = useMutation({
    mutationFn: ({ nextRole, nextStatus }: {
      nextRole: OrganizationRole;
      nextStatus: OrganizationStatus;
    }) => updateOrganizationMember(organizationId, member.user_id, {
      role: nextRole,
      status: nextStatus,
    }),
    onMutate: () => setSaved(false),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: organizationMembersKey(organizationId) });
      setDraft(null);
      setSaved(true);
    },
    onError: (reason) => {
      setDraft(null);
      toast.error(errorMessage(reason));
    },
  });

  const feedback = update.isPending
    ? t('organization.saving', 'Saving…')
    : update.isError
      ? t('organization.saveFailed', 'Save failed')
      : saved
        ? t('organization.saved', 'Saved')
        : '';

  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      <Select
        value={role}
        disabled={update.isPending}
        onValueChange={(nextRole) => {
          const value = nextRole as OrganizationRole;
          setDraft({ role: value, status });
          update.mutate({ nextRole: value, nextStatus: status });
        }}
      >
        <SelectTrigger className="w-36" aria-label={t('organization.role', 'Role')}><SelectValue /></SelectTrigger>
        <SelectContent>
          {ORGANIZATION_ROLES.filter((item) => canAssignAdmin || !['owner', 'admin'].includes(item)).map((item) => (
            <SelectItem key={item} value={item}>{t(`organization.roles.${item}`, item)}</SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Select
        value={status}
        disabled={update.isPending}
        onValueChange={(nextStatus) => {
          const value = nextStatus as OrganizationStatus;
          setDraft({ role, status: value });
          update.mutate({ nextRole: role, nextStatus: value });
        }}
      >
        <SelectTrigger className="w-36" aria-label={t('organization.status', 'Status')}><SelectValue /></SelectTrigger>
        <SelectContent>
          {ORGANIZATION_STATUSES.map((item) => (
            <SelectItem key={item} value={item}>{t(`organization.statuses.${item}`, item)}</SelectItem>
          ))}
        </SelectContent>
      </Select>
      <span
        className={cn(
          'min-w-16 text-right text-xs',
          update.isError ? 'text-destructive' : 'text-muted-foreground',
        )}
        aria-live="polite"
      >
        {feedback}
      </span>
    </div>
  );
}

function ServiceAccountActions({
  organizationId,
  account,
}: {
  organizationId: string;
  account: ServiceAccount;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [confirmAction, setConfirmAction] = useState<'rotate' | 'disable' | null>(null);

  const refresh = () => queryClient.invalidateQueries({
    queryKey: serviceAccountsKey(organizationId),
  });
  const updateStatus = useMutation({
    mutationFn: (status: 'active' | 'disabled') =>
      updateServiceAccountStatus(organizationId, account.service_account_id, status),
    onSuccess: refresh,
    onError: (reason) => toast.error(errorMessage(reason)),
  });
  const rotate = useMutation({
    mutationFn: () => rotateServiceAccountGeneration(organizationId, account.service_account_id),
    onSuccess: refresh,
    onError: (reason) => toast.error(errorMessage(reason)),
  });
  const pending = updateStatus.isPending || rotate.isPending;

  const confirm = () => {
    if (confirmAction === 'rotate') rotate.mutate();
    if (confirmAction === 'disable') updateStatus.mutate('disabled');
    setConfirmAction(null);
  };

  return (
    <>
      <div className="flex items-center gap-2">
        <Button variant="outline" size="sm" disabled={pending} onClick={() => setConfirmAction('rotate')}>
          <RotateCw className={cn(rotate.isPending && 'animate-spin')} />
          {rotate.isPending ? t('organization.rotating', 'Rotating…') : t('organization.rotate', 'Rotate')}
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={pending}
          onClick={() => {
            if (account.status === 'active') setConfirmAction('disable');
            else updateStatus.mutate('active');
          }}
        >
          {updateStatus.isPending
            ? t('organization.saving', 'Saving…')
            : account.status === 'active'
              ? t('organization.disable', 'Disable')
              : t('organization.enable', 'Enable')}
        </Button>
      </div>
      <Dialog open={confirmAction !== null} onOpenChange={(open) => !open && setConfirmAction(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {confirmAction === 'rotate'
                ? t('organization.rotateServiceAccountTitle', 'Rotate this service account?')
                : t('organization.disableServiceAccountTitle', 'Disable this service account?')}
            </DialogTitle>
            <DialogDescription>
              {confirmAction === 'rotate'
                ? t('organization.rotateServiceAccountDescription', 'Existing credentials for this service account will stop working. Tasks, deployments, or integrations using them must be updated.')
                : t('organization.disableServiceAccountDescription', 'Tasks, deployments, or integrations using this identity will fail until it is enabled again.')}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmAction(null)}>{t('common.cancel', 'Cancel')}</Button>
            <Button variant="destructive" onClick={confirm}>
              {confirmAction === 'rotate' ? t('organization.rotate', 'Rotate') : t('organization.disable', 'Disable')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

export function OrganizationSettingsPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const activeOrganizationId = useAuthStore((state) => state.user?.tenant_id ?? '');
  const organizations = useQuery({
    queryKey: organizationsQueryKey,
    queryFn: listOrganizations,
  });
  const activeOrganization = organizations.data?.items.find(
    (item) => item.organization_id === activeOrganizationId,
  );
  const capabilities = useMemo(
    () => new Set(activeOrganization?.access.capabilities ?? []),
    [activeOrganization?.access.capabilities],
  );
  const canManageMembers = capabilities.has('manage_members');
  const canManagePolicy = capabilities.has('manage_policy');
  const canViewDirectory = capabilities.has('view_audit');
  const isBusiness = activeOrganization?.kind === 'business';

  const self = useQuery({
    queryKey: organizationSelfKey(activeOrganizationId),
    queryFn: () => getOrganizationSelf(activeOrganizationId),
    enabled: Boolean(activeOrganizationId && isBusiness),
  });
  const members = useQuery({
    queryKey: organizationMembersKey(activeOrganizationId),
    queryFn: () => listOrganizationMembers(activeOrganizationId),
    enabled: Boolean(activeOrganizationId && isBusiness && canViewDirectory),
  });
  const groups = useQuery({
    queryKey: organizationGroupsKey(activeOrganizationId),
    queryFn: () => listOrganizationGroups(activeOrganizationId),
    enabled: Boolean(activeOrganizationId && isBusiness && canViewDirectory),
  });
  const serviceAccounts = useQuery({
    queryKey: serviceAccountsKey(activeOrganizationId),
    queryFn: () => listServiceAccounts(activeOrganizationId),
    enabled: Boolean(activeOrganizationId && isBusiness && canViewDirectory),
  });

  const [selectedGroupId, setSelectedGroupId] = useState('');
  const effectiveGroupId = groups.data?.some((group) => group.group_id === selectedGroupId)
    ? selectedGroupId
    : groups.data?.[0]?.group_id ?? '';
  const selectedGroup = groups.data?.find((group) => group.group_id === effectiveGroupId);
  const selectedGroupCapabilities = new Set(selectedGroup?.access.capabilities ?? []);
  const canManageSelectedGroup = (
    selectedGroupCapabilities.has('manage_members') && selectedGroup?.source !== 'idp'
  );
  const groupMembers = useQuery({
    queryKey: groupMembersKey(activeOrganizationId, effectiveGroupId),
    queryFn: () => listGroupMembers(activeOrganizationId, effectiveGroupId),
    enabled: Boolean(activeOrganizationId && effectiveGroupId && canViewDirectory),
  });

  const [addCompanyMemberOpen, setAddCompanyMemberOpen] = useState(false);
  const [companyMemberEmail, setCompanyMemberEmail] = useState('');
  const addCompanyMember = useMutation({
    mutationFn: () => addOrganizationMember(activeOrganizationId, companyMemberEmail.trim()),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: organizationMembersKey(activeOrganizationId) });
      setAddCompanyMemberOpen(false);
      setCompanyMemberEmail('');
      toast.success(t('organization.memberAdded', 'Member added'));
    },
    onError: (reason) => toast.error(errorMessage(reason)),
  });

  const [createGroupOpen, setCreateGroupOpen] = useState(false);
  const [groupName, setGroupName] = useState('');
  const [groupKind, setGroupKind] = useState<'department' | 'team'>('team');
  const createGroup = useMutation({
    mutationFn: () => createOrganizationGroup(activeOrganizationId, {
      name: groupName.trim(),
      kind: groupKind,
    }),
    onSuccess: async (group) => {
      await queryClient.invalidateQueries({ queryKey: organizationGroupsKey(activeOrganizationId) });
      setSelectedGroupId(group.group_id);
      setCreateGroupOpen(false);
      setGroupName('');
      toast.success(t('organization.groupCreated', 'Group created'));
    },
    onError: (reason) => toast.error(errorMessage(reason)),
  });

  const [addGroupMemberOpen, setAddGroupMemberOpen] = useState(false);
  const [newGroupUserId, setNewGroupUserId] = useState('');
  const [newGroupRole, setNewGroupRole] = useState<'lead' | 'member'>('member');
  const availableGroupMembers = useMemo(() => {
    const existing = new Set(groupMembers.data?.map((member) => member.user_id) ?? []);
    return members.data?.filter(
      (member) => member.status === 'active' && !existing.has(member.user_id),
    ) ?? [];
  }, [groupMembers.data, members.data]);
  const setGroupMembership = useMutation({
    mutationFn: () => setGroupMember(
      activeOrganizationId,
      effectiveGroupId,
      newGroupUserId,
      { role: newGroupRole, status: 'active' },
    ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: groupMembersKey(activeOrganizationId, effectiveGroupId),
      });
      setAddGroupMemberOpen(false);
      setNewGroupUserId('');
      toast.success(t('organization.memberAdded', 'Member added'));
    },
    onError: (reason) => toast.error(errorMessage(reason)),
  });
  const removeGroupMembership = useMutation({
    mutationFn: (userId: string) => removeGroupMember(
      activeOrganizationId,
      effectiveGroupId,
      userId,
    ),
    onSuccess: () => queryClient.invalidateQueries({
      queryKey: groupMembersKey(activeOrganizationId, effectiveGroupId),
    }),
    onError: (reason) => toast.error(errorMessage(reason)),
  });
  if (organizations.isPending) {
    return <EmptyState>{t('common.loading', 'Loading…')}</EmptyState>;
  }
  if (!activeOrganization || !isBusiness) return null;

  const isManagedMember = self.data?.membership.source === 'scim';
  const role = activeOrganization.role;
  const roleSummary = role === 'owner'
    ? t('organization.roleSummary.owner', 'Owns organization governance and can delegate administrative access.')
    : role === 'admin'
      ? t('organization.roleSummary.admin', 'Manages people, teams, access, and organization policy.')
      : role === 'auditor'
        ? t('organization.roleSummary.auditor', 'Reviews organization structure and operational identity metadata without changing it.')
        : role === 'guest'
          ? t('organization.roleSummary.guest', 'Uses only the teams and resources explicitly shared with this account.')
          : t('organization.roleSummary.member', 'Uses company resources shared through your organization and teams.');

  return (
    <div className="min-w-0">
      <section className="overflow-hidden rounded-2xl border border-edge-subtle bg-surface-raised">
        <div className="flex flex-wrap items-start justify-between gap-5 border-b border-edge-subtle bg-primary/[0.045] px-5 py-5 sm:px-6">
          <div className="flex min-w-0 items-start gap-3.5">
            <span className="grid size-11 shrink-0 place-items-center rounded-2xl bg-primary/10 text-primary shadow-sm">
              <Building2 className="size-5" />
            </span>
            <div className="min-w-0">
              <p className="text-xs font-medium uppercase tracking-[0.12em] text-muted-foreground">
                {t('organization.companyWorkspace', 'Company workspace')}
              </p>
              <h3 className="mt-1 truncate text-lg font-semibold text-content-primary">
                {activeOrganization.name}
              </h3>
              <p className="mt-1 max-w-2xl text-sm leading-6 text-muted-foreground">
                {roleSummary}
              </p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <RoleBadge role={role} />
            <StatusBadge status={activeOrganization.status} />
          </div>
        </div>

        <Tabs defaultValue="overview" className="min-w-0">
          <TabsList
            variant="underline"
            className="h-auto w-full justify-start overflow-x-auto px-5 sm:px-6"
            aria-label={t('organization.sections.label', 'Organization sections')}
          >
            <TabsTrigger value="overview" className="py-3">
              {t('organization.sections.overview', 'Overview')}
            </TabsTrigger>
            {canViewDirectory ? (
              <TabsTrigger value="people" className="py-3">
                {t('organization.sections.people', 'People')}
              </TabsTrigger>
            ) : null}
            <TabsTrigger value="teams" className="py-3">
              {canViewDirectory
                ? t('organization.sections.teams', 'Departments & teams')
                : t('organization.sections.myTeams', 'My teams')}
            </TabsTrigger>
            {canManagePolicy ? (
              <TabsTrigger value="identity" className="py-3">
                {t('organization.sections.identity', 'Security & identity')}
              </TabsTrigger>
            ) : null}
            {canViewDirectory ? (
              <TabsTrigger value="operations" className="py-3">
                {t('organization.sections.operations', 'Operations')}
              </TabsTrigger>
            ) : null}
          </TabsList>

          <TabsContent value="overview" className="m-0 p-5 sm:p-6">
            <div className="grid gap-4 lg:grid-cols-[minmax(0,1.25fr)_minmax(240px,0.75fr)]">
              <section className="rounded-xl border border-edge-subtle bg-surface-work p-5">
                <div className="flex items-center gap-2 text-sm font-semibold">
                  <ShieldCheck className="size-4 text-primary" />
                  {t('organization.yourMembership', 'Your membership')}
                </div>
                <dl className="mt-4 grid gap-4 sm:grid-cols-2">
                  <div>
                    <dt className="text-xs text-muted-foreground">{t('organization.role', 'Role')}</dt>
                    <dd className="mt-1 text-sm font-medium">{t(`organization.roles.${role}`, role)}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted-foreground">{t('organization.accountSource', 'Account source')}</dt>
                    <dd className="mt-1 text-sm font-medium">
                      {isManagedMember ? t('organization.source.scim', 'Company directory (SCIM)') : t('organization.source.native', 'Direct membership')}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted-foreground">{t('organization.membershipStatus', 'Membership status')}</dt>
                    <dd className="mt-1"><StatusBadge status={activeOrganization.status} /></dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted-foreground">{t('organization.workspaceType', 'Workspace type')}</dt>
                    <dd className="mt-1 text-sm font-medium">{t('organization.kinds.business', 'Company')}</dd>
                  </div>
                </dl>
                {isManagedMember ? (
                  <p className="mt-5 rounded-lg border border-primary/10 bg-primary/[0.045] px-3.5 py-3 text-xs leading-5 text-muted-foreground">
                    {t('organization.directoryManagedHint', 'Your company directory manages this account, department membership, and sign-in policy. Contact an organization administrator to request changes.')}
                  </p>
                ) : null}
              </section>

              <section className="rounded-xl border border-edge-subtle bg-surface-work p-5">
                <div className="flex items-center gap-2 text-sm font-semibold">
                  <UsersRound className="size-4 text-primary" />
                  {t('organization.teamMemberships', 'Team memberships')}
                </div>
                <div className="mt-3 space-y-2">
                  {self.data?.groups.length ? self.data.groups.map((group) => (
                    <div key={group.group_id} className="flex items-center gap-3 rounded-lg bg-surface-sunken/55 px-3 py-2.5">
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-medium">{group.name}</span>
                        <span className="block text-xs text-muted-foreground">
                          {t(`organization.groupKinds.${group.kind}`, group.kind)}
                          {group.source === 'idp' ? ' · IdP' : ''}
                        </span>
                      </span>
                      <RoleBadge role={group.role} />
                    </div>
                  )) : (
                    <p className="py-6 text-center text-sm text-muted-foreground">
                      {t('organization.noTeamMemberships', 'You are not assigned to a department or team yet.')}
                    </p>
                  )}
                </div>
              </section>
            </div>
          </TabsContent>

          {canViewDirectory ? (
            <TabsContent value="people" className="m-0 p-5 sm:p-6">
              <div className="mb-4">
                <div className="flex items-center justify-between gap-3">
                <h4 className="text-sm font-semibold">{t('organization.members', 'People')}</h4>
                  {canManageMembers && isBusiness ? <Button variant="outline" size="sm" onClick={() => setAddCompanyMemberOpen(true)}><Plus />{t('organization.addCompanyMember', 'Add company member')}</Button> : null}
                </div>
                <p className="mt-1 text-sm text-muted-foreground">
                  {canManageMembers
                    ? t('organization.peopleManageHint', 'Manage organization roles and membership status. Directory-managed members remain read only.')
                    : t('organization.peopleReadOnlyHint', 'Read-only organization membership directory.')}
                </p>
              </div>
              <div className="overflow-hidden rounded-xl border border-edge-subtle">
                {members.data?.map((member) => {
                  const editable = canManageMembers && member.source !== 'scim' && (activeOrganization?.role === 'owner' || !['owner', 'admin'].includes(member.role));
                  return (
                    <div key={member.membership_id} className="flex flex-wrap items-center gap-3 border-b border-edge-subtle px-4 py-3 last:border-b-0">
                      <div className="min-w-[220px] flex-1">
                        <MemberIdentity member={member} />
                      </div>
                      {member.source === 'scim' ? (
                        <span className="rounded-full bg-primary/10 px-2 py-1 text-xs text-primary">SCIM</span>
                      ) : null}
                      {editable ? (
                        <MemberAccessControls organizationId={activeOrganizationId} member={member} canAssignAdmin={activeOrganization?.role === 'owner'} />
                      ) : (
                        <div className="flex items-center gap-2">
                          <RoleBadge role={member.role} />
                          <StatusBadge status={member.status} />
                        </div>
                      )}
                    </div>
                  );
                })}
                {members.data?.length === 0 ? (
                  <p className="p-8 text-center text-sm text-muted-foreground">{t('organization.noMembers', 'No members found.')}</p>
                ) : null}
              </div>
            </TabsContent>
          ) : null}

          <TabsContent value="teams" className="m-0 p-5 sm:p-6">
            {!canViewDirectory ? (
              <div className="grid gap-3 sm:grid-cols-2">
                {self.data?.groups.length ? self.data.groups.map((group) => (
                  <article key={group.group_id} className="rounded-xl border border-edge-subtle bg-surface-work p-4">
                    <div className="flex items-start justify-between gap-3">
                      <span className="grid size-9 place-items-center rounded-xl bg-primary/[0.08] text-primary"><UsersRound className="size-4" /></span>
                      <RoleBadge role={group.role} />
                    </div>
                    <h4 className="mt-4 text-sm font-semibold">{group.name}</h4>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {t(`organization.groupKinds.${group.kind}`, group.kind)}{group.source === 'idp' ? ' · IdP' : ''}
                    </p>
                  </article>
                )) : <div className="sm:col-span-2"><EmptyState>{t('organization.noTeamMemberships', 'You are not assigned to a department or team yet.')}</EmptyState></div>}
              </div>
            ) : (
              <div>
                <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h4 className="text-sm font-semibold">{t('organization.groups', 'Departments & teams')}</h4>
                    <p className="mt-1 text-sm text-muted-foreground">{t('organization.groupsDescription', 'Organize people and inherited resource access without exposing private user content.')}</p>
                  </div>
                  {canManageMembers ? (
                    <Button variant="outline" size="sm" onClick={() => setCreateGroupOpen(true)}><Plus />{t('organization.newGroup', 'New group')}</Button>
                  ) : null}
                </div>
                <div className="grid min-h-[320px] gap-4 lg:grid-cols-[230px_minmax(0,1fr)]">
                  <div className="overflow-hidden rounded-xl border border-edge-subtle">
                    {groups.data?.map((group) => (
                      <button
                        type="button"
                        key={group.group_id}
                        className={cn(
                          'flex w-full items-center gap-2 border-b border-edge-subtle px-3 py-3 text-left text-sm transition-colors last:border-b-0 hover:bg-surface-hover',
                          effectiveGroupId === group.group_id && 'bg-primary/[0.07] text-foreground',
                        )}
                        onClick={() => setSelectedGroupId(group.group_id)}
                      >
                        <UsersRound className="size-4 shrink-0" />
                        <span className="min-w-0 flex-1 truncate">{group.name}</span>
                        {group.source === 'idp' ? <span className="text-xs text-primary">IdP</span> : null}
                      </button>
                    ))}
                    {groups.data?.length === 0 ? <p className="p-4 text-sm text-muted-foreground">{t('organization.noGroups', 'No groups yet.')}</p> : null}
                  </div>
                  <div className="overflow-hidden rounded-xl border border-edge-subtle">
                    <div className="flex items-center justify-between gap-3 border-b border-edge-subtle bg-surface-sunken/35 px-4 py-3">
                      <div>
                        <p className="text-sm font-semibold">{selectedGroup?.name ?? t('organization.selectGroup', 'Select a group')}</p>
                        {selectedGroup ? <p className="mt-0.5 text-xs text-muted-foreground">{t(`organization.groupKinds.${selectedGroup.kind}`, selectedGroup.kind)}{selectedGroup.source === 'idp' ? ` · ${t('organization.idpManagedReadOnly', 'Managed by the identity provider')}` : ''}</p> : null}
                      </div>
                      {selectedGroup && canManageSelectedGroup && canManageMembers ? (
                        <Button variant="outline" size="sm" onClick={() => setAddGroupMemberOpen(true)}><Plus />{t('organization.addMember', 'Add member')}</Button>
                      ) : null}
                    </div>
                    {groupMembers.data?.map((member) => (
                      <div key={member.membership_id} className="flex items-center gap-3 border-b border-edge-subtle px-4 py-3 last:border-b-0">
                        <div className="min-w-0 flex-1"><p className="truncate text-sm font-medium">{member.display_name || member.email}</p><p className="truncate text-xs text-muted-foreground">{member.email}</p></div>
                        <RoleBadge role={member.role} />
                        {canManageSelectedGroup && canManageMembers ? (
                          <Button variant="ghost" size="icon-sm" aria-label={t('organization.removeMember', 'Remove member')} onClick={() => removeGroupMembership.mutate(member.user_id)}><Trash2 /></Button>
                        ) : null}
                      </div>
                    ))}
                    {selectedGroup && groupMembers.data?.length === 0 ? <p className="p-8 text-center text-sm text-muted-foreground">{t('organization.noGroupMembers', 'No members in this group.')}</p> : null}
                  </div>
                </div>
              </div>
            )}
          </TabsContent>

          {canManagePolicy ? (
            <TabsContent value="identity" className="m-0 p-5 sm:p-6">
              <EnterpriseIdentityPanel organizationId={activeOrganizationId} />
            </TabsContent>
          ) : null}

          {canViewDirectory ? (
            <TabsContent value="operations" className="m-0 p-5 sm:p-6">
              <div className="mb-4 flex items-start gap-3">
                <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-primary/[0.08] text-primary"><KeyRound className="size-4" /></span>
                <div><h4 className="text-sm font-semibold">{t('organization.serviceAccounts', 'Service accounts')}</h4><p className="mt-1 max-w-3xl text-sm text-muted-foreground">{t('organization.serviceAccountsDescription', 'Non-interactive identities are scoped to one task, deployment, or integration. They cannot sign in as people.')}</p></div>
              </div>
              <div className="overflow-hidden rounded-xl border border-edge-subtle">
                {serviceAccounts.data?.map((account) => (
                  <div key={account.service_account_id} className="flex flex-wrap items-center gap-3 border-b border-edge-subtle px-4 py-3 last:border-b-0">
                    <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-surface-sunken text-content-secondary"><LockKeyhole className="size-4" /></span>
                    <div className="min-w-[220px] flex-1"><p className="truncate text-sm font-medium">{account.name}</p><p className="truncate text-xs text-muted-foreground">{account.kind} · {account.owner_resource_type}:{account.owner_resource_id} · {t('organization.serviceAccounts.generation', 'generation')} {account.generation}</p></div>
                    <StatusBadge status={account.status} />
                    {canManagePolicy ? (
                      <ServiceAccountActions organizationId={activeOrganizationId} account={account} />
                    ) : null}
                  </div>
                ))}
                {serviceAccounts.data?.length === 0 ? <p className="p-8 text-center text-sm text-muted-foreground">{t('organization.noServiceAccounts', 'No service accounts yet. They are created automatically for scheduled tasks and deployments.')}</p> : null}
              </div>
            </TabsContent>
          ) : null}
        </Tabs>
      </section>

      <Dialog open={addCompanyMemberOpen} onOpenChange={setAddCompanyMemberOpen}>
        <DialogContent>
          <form onSubmit={(event) => { event.preventDefault(); if (canManageMembers && isBusiness && !addCompanyMember.isPending) addCompanyMember.mutate(); }}>
            <DialogHeader>
              <DialogTitle>{t('organization.addCompanyMember', 'Add company member')}</DialogTitle>
              <DialogDescription>{t('organization.addCompanyMemberHint', 'Enter the email of a registered account. They join as an ordinary member; their personal resources remain private.')}</DialogDescription>
            </DialogHeader>
            <div className="grid gap-2 py-4">
              <Label htmlFor="company-member-email">{t('organization.memberEmail', 'Email')}</Label>
              <Input id="company-member-email" type="email" required autoComplete="off" value={companyMemberEmail} onChange={(event) => setCompanyMemberEmail(event.target.value)} />
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setAddCompanyMemberOpen(false)}>{t('common_cancel', 'Cancel')}</Button>
              <Button type="submit" disabled={!canManageMembers || !isBusiness || !companyMemberEmail.trim() || addCompanyMember.isPending}>{t('organization.addCompanyMember', 'Add company member')}</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={createGroupOpen} onOpenChange={setCreateGroupOpen}>
        <DialogContent>
          <DialogHeader><DialogTitle>{t('organization.newGroup', 'New group')}</DialogTitle><DialogDescription>{t('organization.newGroupDescription', 'Create a department or team in this workspace.')}</DialogDescription></DialogHeader>
          <div className="grid gap-4 py-2">
            <div className="grid gap-2"><Label htmlFor="organization-group-name">{t('organization.name', 'Name')}</Label><Input id="organization-group-name" value={groupName} onChange={(event) => setGroupName(event.target.value)} /></div>
            <div className="grid gap-2"><Label>{t('organization.type', 'Type')}</Label><Select value={groupKind} onValueChange={(value) => setGroupKind(value as 'department' | 'team')}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="team">{t('organization.groupKinds.team', 'Team')}</SelectItem><SelectItem value="department">{t('organization.groupKinds.department', 'Department')}</SelectItem></SelectContent></Select></div>
          </div>
          <DialogFooter><Button variant="outline" onClick={() => setCreateGroupOpen(false)}>{t('common_cancel', 'Cancel')}</Button><Button disabled={!groupName.trim() || createGroup.isPending} onClick={() => createGroup.mutate()}>{t('common_create', 'Create')}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={addGroupMemberOpen} onOpenChange={setAddGroupMemberOpen}>
        <DialogContent>
          <DialogHeader><DialogTitle>{t('organization.addMember', 'Add member')}</DialogTitle><DialogDescription>{t('organization.addMemberDescription', 'Choose an active organization member and their team role.')}</DialogDescription></DialogHeader>
          <div className="grid gap-4 py-2">
            <Select value={newGroupUserId} onValueChange={setNewGroupUserId}><SelectTrigger><SelectValue placeholder={t('organization.chooseMember', 'Choose a member')} /></SelectTrigger><SelectContent>{availableGroupMembers.map((member) => <SelectItem key={member.user_id} value={member.user_id}>{member.display_name || member.email}</SelectItem>)}</SelectContent></Select>
            <Select value={newGroupRole} onValueChange={(value) => setNewGroupRole(value as 'lead' | 'member')}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="member">{t('organization.roles.member', 'Member')}</SelectItem><SelectItem value="lead">{t('organization.roles.lead', 'Team lead')}</SelectItem></SelectContent></Select>
          </div>
          <DialogFooter><Button variant="outline" onClick={() => setAddGroupMemberOpen(false)}>{t('common_cancel', 'Cancel')}</Button><Button disabled={!newGroupUserId || setGroupMembership.isPending} onClick={() => setGroupMembership.mutate()}><CheckCircle2 />{t('organization.addMember', 'Add member')}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
