import { useId, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Building2,
  Search,
  ShieldCheck,
  Trash2,
  UserRound,
  UsersRound,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  grantResolvedResourceBinding,
  listOrganizations,
  listOrganizationGroups,
  listResourceBindings,
  resolveResourceShareTarget,
  revokeResourceBinding,
  type DirectBinding,
  type ResolvedShareTarget,
  type ShareableResourceKind,
  type ShareRelation,
  type ShareTargetType,
} from '@/lib/api/organizations';
import { organizationsQueryKey } from '@/lib/api/organization-query-keys';
import { useAuthStore } from '@/stores/auth';

export interface ResourceShareDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  resourceKind: ShareableResourceKind;
  resourceId: string;
  resourceName: string;
  effectiveRole?: string | null;
  accessSource?: string;
}

const RELATIONS: readonly ShareRelation[] = [
  'viewer',
  'editor',
  'operator',
  'manager',
];

function targetIcon(type: ShareTargetType) {
  if (type === 'group') return UsersRound;
  if (type === 'organization') return Building2;
  return UserRound;
}

export function ResourceShareDialog({
  open,
  onOpenChange,
  resourceKind,
  resourceId,
  resourceName,
  effectiveRole,
  accessSource,
}: ResourceShareDialogProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const identifierId = useId();
  const organizationId = useAuthStore((state) => state.user?.tenant_id ?? '');
  const organizations = useQuery({
    queryKey: organizationsQueryKey,
    queryFn: listOrganizations,
    enabled: open && Boolean(organizationId),
  });
  const activeOrganization = organizations.data?.items.find(
    (item) => item.organization_id === organizationId,
  );
  const isBusiness = activeOrganization?.kind === 'business';
  const bindingKey = [
    'resource-access',
    organizationId,
    resourceKind,
    resourceId,
  ] as const;
  const bindings = useQuery({
    queryKey: bindingKey,
    queryFn: () => listResourceBindings(resourceKind, resourceId),
    enabled: open && Boolean(resourceId),
    retry: false,
  });
  const [targetType, setTargetType] = useState<ShareTargetType>('user');
  const [identifier, setIdentifier] = useState('');
  const [resolvedTarget, setResolvedTarget] = useState<ResolvedShareTarget | null>(null);
  const [relation, setRelation] = useState<ShareRelation>('viewer');

  const groups = useQuery({
    queryKey: ['organization-groups', organizationId],
    queryFn: () => listOrganizationGroups(organizationId),
    enabled: open && isBusiness && targetType === 'group',
  });
  const groupOptions = (groups.data ?? []).flatMap((group) => {
    const names = [group.name];
    const seen = new Set([group.group_id]);
    let parentId = group.parent_group_id;
    while (parentId) {
      const parent = groups.data?.find((item) => item.group_id === parentId);
      if (!parent || seen.has(parentId) || names.length >= 8) return [];
      seen.add(parentId);
      names.unshift(parent.name);
      parentId = parent.parent_group_id;
    }
    const path = names.join(' / ');
    return path.toLocaleLowerCase().includes(identifier.trim().toLocaleLowerCase())
      ? [{ id: group.group_id, path }] : [];
  });

  const resetTarget = () => {
    setTargetType('user');
    setIdentifier('');
    setResolvedTarget(null);
    setRelation('viewer');
  };

  const resolveTarget = useMutation({
    mutationFn: (selectedPath?: string) => resolveResourceShareTarget(
      resourceKind,
      resourceId,
      {
        target_type: targetType,
        identifier: targetType === 'organization' ? '' : selectedPath ?? identifier.trim(),
      },
    ),
    onSuccess: (target) => {
      setResolvedTarget(target);
      if (target === null) {
        toast.error(t(
          'share.targetNotFound',
          'No eligible account or department/team matched that exact value.',
        ));
        return;
      }
      setRelation(target.allowed_relations[0] ?? 'viewer');
    },
    onError: (reason) => toast.error(
      reason instanceof Error
        ? reason.message
        : t('share.searchFailed', 'Could not search for that access target.'),
    ),
  });

  const grant = useMutation({
    mutationFn: () => {
      if (resolvedTarget === null) {
        throw new Error('share_target_must_be_resolved');
      }
      return grantResolvedResourceBinding(
        resourceKind,
        resourceId,
        relation,
        resolvedTarget.resolution_token,
      );
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: bindingKey });
      setIdentifier('');
      setResolvedTarget(null);
      setRelation('viewer');
      toast.success(t('share.granted', 'Access granted'));
    },
    onError: (reason) => toast.error(
      reason instanceof Error
        ? reason.message
        : t('share.changeFailed', 'Access change failed'),
    ),
  });

  const revoke = useMutation({
    mutationFn: (binding: DirectBinding) => revokeResourceBinding(
      resourceKind,
      resourceId,
      {
        relation: binding.relation,
        subject_type: binding.subject_type,
        subject_id: binding.subject_id,
        subject_relation: binding.subject_relation,
      },
    ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: bindingKey });
      toast.success(t('share.revoked', 'Access revoked'));
    },
    onError: (reason) => toast.error(
      reason instanceof Error
        ? reason.message
        : t('share.changeFailed', 'Access change failed'),
    ),
  });

  const updateTargetType = (value: string) => {
    setTargetType(value as ShareTargetType);
    setIdentifier('');
    setResolvedTarget(null);
    setRelation('viewer');
  };
  const updateIdentifier = (value: string) => {
    setIdentifier(value);
    setResolvedTarget(null);
    setRelation('viewer');
  };
  const canSearch = targetType === 'organization' || Boolean(identifier.trim());
  const allowedRelations = resolvedTarget?.allowed_relations.filter(
    (item): item is ShareRelation => RELATIONS.includes(item),
  ) ?? [];
  const effectiveRoleLabel = effectiveRole && RELATIONS.includes(effectiveRole as ShareRelation)
    ? t(`share.role.${effectiveRole}`, effectiveRole)
    : effectiveRole ?? t('share.customAccess', 'Custom access');
  const accessSourceLabel = accessSource === 'computed'
    ? t('share.computed', 'Computed')
    : accessSource ?? t('share.computed', 'Computed');

  const bindingLabel = (binding: DirectBinding) => {
    if (binding.display_name?.trim()) return binding.display_name;
    if (binding.subject_type === 'organization') {
      return t('share.entireOrganization', 'Entire organization');
    }
    if (binding.subject_type === 'group') {
      return t('share.departmentTeam', 'Department/Team');
    }
    if (binding.subject_type === 'service_account') {
      return t('share.serviceAccount', 'Service account');
    }
    return t('share.user', 'User');
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) resetTarget();
        onOpenChange(nextOpen);
      }}
    >
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t('share.resourceTitle', 'Share resource')}</DialogTitle>
          <DialogDescription>
            {t('share.description', 'Manage access to “{{name}}”.', { name: resourceName })}
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-5">
          <div className="flex items-start gap-3 rounded-xl border border-edge-subtle bg-surface-sunken/55 px-4 py-3">
            <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary/10 text-primary">
              <ShieldCheck className="size-4.5" aria-hidden="true" />
            </span>
            <div className="min-w-0">
              <p className="text-sm font-medium">
                {t('share.effectiveAccess', 'Your effective access')}
              </p>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {effectiveRoleLabel} · {accessSourceLabel}
              </p>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                {t(
                  'share.privacyHint',
                  'Enter an exact email address or department/team path, then search explicitly. No account directory is shown.',
                )}
              </p>
            </div>
          </div>

          <section className="grid gap-3" aria-labelledby="share-add-title">
            <div>
              <h3 id="share-add-title" className="text-sm font-semibold">
                {t('share.addDirect', 'Add access')}
              </h3>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {isBusiness
                  ? t(
                      'share.businessTargetHint',
                      'Search a company member by full email, a department/team by its exact path, or choose the entire organization.',
                    )
                  : t(
                      'share.personalTargetHint',
                      'Search another account by its complete email address.',
                    )}
              </p>
            </div>
            <div className="grid gap-3 sm:grid-cols-[160px_minmax(0,1fr)_auto] sm:items-end">
              <div className="grid gap-1.5">
                <Label>{t('share.targetType', 'Target type')}</Label>
                <Select value={targetType} onValueChange={updateTargetType}>
                  <SelectTrigger aria-label={t('share.targetType', 'Target type')}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="user">{t('share.user', 'User')}</SelectItem>
                    {isBusiness ? (
                      <>
                        <SelectItem value="group">
                          {t('share.departmentTeam', 'Department/Team')}
                        </SelectItem>
                        <SelectItem value="organization">
                          {t('share.entireOrganization', 'Entire organization')}
                        </SelectItem>
                      </>
                    ) : null}
                  </SelectContent>
                </Select>
              </div>
              {targetType === 'organization' ? (
                <div className="flex h-10 items-center rounded-md border border-edge-subtle bg-surface-sunken px-3 text-sm text-muted-foreground">
                  {activeOrganization?.name ?? t('share.currentOrganization', 'Current organization')}
                </div>
              ) : (
                <div className="grid gap-1.5">
                  <Label htmlFor={identifierId}>
                    {targetType === 'user'
                      ? t('share.emailAddress', 'Complete email address')
                      : t('share.departmentPath', 'Exact department/team path')}
                  </Label>
                  <Input
                    id={identifierId}
                    value={identifier}
                    inputMode={targetType === 'user' ? 'email' : 'text'}
                    autoComplete="off"
                    placeholder={targetType === 'user'
                      ? t('share.emailPlaceholder', 'name@example.com')
                      : t('share.groupPlaceholder', 'Product / Platform')}
                    onChange={(event) => updateIdentifier(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' && canSearch && !resolveTarget.isPending) {
                        event.preventDefault();
                        resolveTarget.mutate(undefined);
                      }
                    }}
                  />
                </div>
              )}
              <Button
                variant="outline"
                disabled={!canSearch || resolveTarget.isPending}
                onClick={() => resolveTarget.mutate(undefined)}
              >
                <Search className="size-4" />
                {resolveTarget.isPending
                  ? t('share.searching', 'Searching…')
                  : t('share.searchAction', 'Search')}
              </Button>
            </div>

            {targetType === 'group' && !resolvedTarget ? (
              <div className="max-h-48 overflow-y-auto rounded-lg border border-edge-subtle">
                {groups.isLoading ? <p className="p-3 text-sm">{t('share.searching', 'Searching…')}</p> : groups.isError ? <p className="p-3 text-sm text-destructive">{t('share.searchFailed')}</p> : groupOptions.length === 0 ? <p className="p-3 text-sm">{t('share.noDepartments', 'No matching departments or teams.')}</p> : groupOptions.map((group) => (
                  <button key={group.id} type="button" disabled={resolveTarget.isPending} className="block w-full px-3 py-2 text-left text-sm hover:bg-accent disabled:opacity-50" onClick={() => { updateIdentifier(group.path); resolveTarget.mutate(group.path); }}>{group.path}</button>
                ))}
              </div>
            ) : null}

            {resolvedTarget ? (
              <div className="flex flex-col gap-3 rounded-xl border border-primary/25 bg-primary/[0.045] p-3 sm:flex-row sm:items-center">
                {(() => {
                  const Icon = targetIcon(resolvedTarget.target_type);
                  return (
                    <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-primary/10 text-primary">
                      <Icon className="size-4.5" aria-hidden="true" />
                    </span>
                  );
                })()}
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium">{resolvedTarget.display_name}</p>
                  {resolvedTarget.detail ? (
                    <p className="mt-0.5 truncate text-xs text-muted-foreground">
                      {resolvedTarget.detail}
                    </p>
                  ) : null}
                </div>
                <Select
                  value={relation}
                  onValueChange={(value) => setRelation(value as ShareRelation)}
                >
                  <SelectTrigger className="w-full sm:w-36" aria-label={t('share.role', 'Access role')}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {allowedRelations.map((item) => (
                      <SelectItem key={item} value={item}>
                        {t(`share.role.${item}`, item)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Button
                  disabled={!allowedRelations.includes(relation) || grant.isPending}
                  onClick={() => grant.mutate()}
                >
                  {grant.isPending
                    ? t('share.adding', 'Adding…')
                    : t('share.confirmAdd', 'Add')}
                </Button>
              </div>
            ) : null}
          </section>

          <section className="grid gap-2" aria-labelledby="share-direct-title">
            <div>
              <h3 id="share-direct-title" className="text-sm font-semibold">
                {t('share.directAccess', 'Direct access')}
              </h3>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {t(
                  'share.inheritedHint',
                  'Inherited access is managed through its department, team, or organization.',
                )}
              </p>
            </div>
            <div className="max-h-56 divide-y divide-edge-subtle overflow-auto rounded-xl border border-edge-subtle">
              {bindings.isLoading ? (
                <p className="p-4 text-sm text-muted-foreground">
                  {t('common.loading', 'Loading…')}
                </p>
              ) : bindings.isError ? (
                <p className="p-4 text-sm text-destructive">
                  {t('share.loadFailed', 'Could not load direct access.')}
                </p>
              ) : bindings.data?.length ? bindings.data.map((binding) => {
                const Icon = binding.subject_type === 'group'
                  ? UsersRound
                  : binding.subject_type === 'organization'
                    ? Building2
                    : UserRound;
                return (
                  <div
                    key={`${binding.relation}:${binding.subject_type}:${binding.subject_id}:${binding.subject_relation ?? ''}`}
                    className="flex items-center gap-3 px-3 py-2.5"
                  >
                    <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-surface-sunken text-muted-foreground">
                      <Icon className="size-4" aria-hidden="true" />
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium">
                        {bindingLabel(binding)}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {binding.detail ? `${binding.detail} · ` : null}
                        {t(`share.role.${binding.relation}`, binding.relation)}
                      </p>
                    </div>
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      disabled={revoke.isPending}
                      aria-label={t('share.revoke', 'Revoke access')}
                      onClick={() => revoke.mutate(binding)}
                    >
                      <Trash2 />
                    </Button>
                  </div>
                );
              }) : (
                <p className="p-4 text-sm text-muted-foreground">
                  {t('share.noDirectAccess', 'No direct access entries.')}
                </p>
              )}
            </div>
          </section>
        </div>
      </DialogContent>
    </Dialog>
  );
}
