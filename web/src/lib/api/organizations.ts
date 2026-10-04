import { resolveApiUrl } from '@/lib/base-path';
import { useAuthStore } from '@/stores/auth';

export type OrganizationRole = 'owner' | 'admin' | 'member' | 'guest' | 'auditor';
export type OrganizationStatus = 'invited' | 'active' | 'suspended' | 'revoking' | 'revoked';
export type ResourceAction =
  | 'view_metadata'
  | 'view'
  | 'export'
  | 'create'
  | 'update'
  | 'delete'
  | 'transfer'
  | 'manage_access'
  | 'use'
  | 'execute'
  | 'cancel'
  | 'resume'
  | 'inspect_runs'
  | 'deploy'
  | 'mount'
  | 'publish'
  | 'manage_secret'
  | 'manage_members'
  | 'manage_policy'
  | 'view_audit';

export interface ResourceAccess {
  capabilities: ResourceAction[];
  effective_role: string | null;
  source: string;
}

export interface Organization {
  organization_id: string;
  kind: 'personal' | 'business';
  slug: string;
  name: string;
  membership_id: string;
  role: OrganizationRole;
  status: OrganizationStatus;
  active: boolean;
  access: ResourceAccess;
}

export interface OrganizationMember {
  membership_id: string;
  user_id: string;
  email: string;
  display_name: string;
  role: OrganizationRole;
  status: OrganizationStatus;
  source: 'native' | 'scim';
  directory_provider_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface OrganizationGroup {
  group_id: string;
  organization_id: string;
  parent_group_id: string | null;
  kind: 'department' | 'team';
  name: string;
  source: 'native' | 'idp';
  directory_provider_id: string | null;
  external_id: string | null;
  status: 'active' | 'archived';
  created_by: string;
  created_at: string;
  updated_at: string;
  access: ResourceAccess;
}

export interface GroupMember {
  membership_id: string;
  user_id: string;
  email: string;
  display_name: string;
  role: 'lead' | 'member';
  status: 'active' | 'suspended' | 'revoked';
  created_at: string;
  updated_at: string;
}

export interface OrganizationSelfGroup {
  group_id: string;
  kind: 'department' | 'team';
  name: string;
  source: 'native' | 'idp';
  role: 'lead' | 'member';
  status: 'active' | 'suspended' | 'revoked';
}

export interface OrganizationSelf {
  membership: OrganizationMember;
  groups: OrganizationSelfGroup[];
}

export interface ServiceAccount {
  service_account_id: string;
  name: string;
  kind: 'deployment' | 'schedule' | 'task' | 'integration';
  owner_resource_type: 'deployment' | 'task' | 'integration';
  owner_resource_id: string;
  status: 'active' | 'disabled' | 'deleted';
  generation: number;
  created_by: string;
  credential_ids: string[];
  created_at: string;
  updated_at: string;
  disabled_at: string | null;
}

export type ShareRelation = 'viewer' | 'editor' | 'operator' | 'manager';
export type ShareSubjectType = 'user' | 'group' | 'organization' | 'service_account';
export type ShareableResourceKind =
  | 'workflow'
  | 'task'
  | 'deployment'
  | 'knowledge_base'
  | 'skill_installation';

export interface DirectBinding {
  relation: ShareRelation;
  subject_type: ShareSubjectType;
  subject_id: string;
  subject_relation: 'direct_member' | 'member' | null;
  source: 'direct';
  display_name?: string;
  detail?: string;
}

export type ShareTargetType = 'user' | 'group' | 'organization';

export interface ResolvedShareTarget {
  target_type: ShareTargetType;
  display_name: string;
  detail: string;
  resolution_token: string;
  allowed_relations: ShareRelation[];
}

export interface ResourceParty {
  type: 'user' | 'organization' | 'platform';
  display_name: string;
}

export interface ResourceProvenance {
  ownership_scope: 'personal' | 'organization' | 'platform';
  origin_type:
    | 'created'
    | 'uploaded'
    | 'imported'
    | 'catalog_install'
    | 'derived'
    | 'system';
  owner: ResourceParty;
  created_by?: ResourceParty | null;
}

export interface SharedResource {
  resource_type: ShareableResourceKind;
  resource_id: string;
  name: string;
  description: string;
  updated_at: string;
  access: ResourceAccess;
  provenance: ResourceProvenance;
}

export class OrganizationApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(
    status: number,
    detail: string,
  ) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(resolveApiUrl(path), {
    credentials: 'include',
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  });
  if (response.status === 401) {
    useAuthStore.getState().handle401();
  }
  const payload = await response.json().catch(() => null) as
    | { detail?: unknown }
    | null;
  if (!response.ok) {
    const detail = payload?.detail;
    throw new OrganizationApiError(
      response.status,
      typeof detail === 'string'
        ? detail
        : `organization_request_failed_${response.status}`,
    );
  }
  return payload as T;
}

export async function listOrganizations(): Promise<{
  items: Organization[];
  active_organization_id: string;
  session_generation: number;
}> {
  return requestJson('/api/v1/organizations');
}

export async function createOrganization(input: {
  name: string;
  slug: string;
}): Promise<Organization> {
  return requestJson('/api/v1/organizations', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export async function addOrganizationMember(
  organizationId: string,
  email: string,
): Promise<OrganizationMember> {
  return requestJson(`/api/v1/organizations/${encodeURIComponent(organizationId)}/members`, {
    method: 'POST',
    body: JSON.stringify({ email }),
  });
}

export async function listOrganizationMembers(
  organizationId: string,
): Promise<OrganizationMember[]> {
  const data = await requestJson<{ items: OrganizationMember[] }>(
    `/api/v1/organizations/${encodeURIComponent(organizationId)}/members`,
  );
  return data.items;
}

export async function getOrganizationSelf(
  organizationId: string,
): Promise<OrganizationSelf> {
  return requestJson(
    `/api/v1/organizations/${encodeURIComponent(organizationId)}/me`,
  );
}

export async function updateOrganizationMember(
  organizationId: string,
  userId: string,
  input: Pick<OrganizationMember, 'role' | 'status'>,
): Promise<OrganizationMember> {
  return requestJson(
    `/api/v1/organizations/${encodeURIComponent(organizationId)}/members/${encodeURIComponent(userId)}`,
    { method: 'PATCH', body: JSON.stringify(input) },
  );
}

export async function listOrganizationGroups(
  organizationId: string,
): Promise<OrganizationGroup[]> {
  const data = await requestJson<{ items: OrganizationGroup[] }>(
    `/api/v1/organizations/${encodeURIComponent(organizationId)}/groups`,
  );
  return data.items;
}

export async function createOrganizationGroup(
  organizationId: string,
  input: {
    name: string;
    kind: 'department' | 'team';
    parent_group_id?: string | null;
  },
): Promise<OrganizationGroup> {
  return requestJson(
    `/api/v1/organizations/${encodeURIComponent(organizationId)}/groups`,
    { method: 'POST', body: JSON.stringify(input) },
  );
}

export async function updateOrganizationGroup(
  organizationId: string,
  groupId: string,
  input: Partial<Pick<OrganizationGroup, 'name' | 'kind' | 'parent_group_id'>>,
): Promise<OrganizationGroup> {
  return requestJson(
    `/api/v1/organizations/${encodeURIComponent(organizationId)}/groups/${encodeURIComponent(groupId)}`,
    { method: 'PATCH', body: JSON.stringify(input) },
  );
}

export async function archiveOrganizationGroup(
  organizationId: string,
  groupId: string,
): Promise<void> {
  await requestJson<unknown>(
    `/api/v1/organizations/${encodeURIComponent(organizationId)}/groups/${encodeURIComponent(groupId)}`,
    { method: 'DELETE' },
  );
}

export async function listGroupMembers(
  organizationId: string,
  groupId: string,
): Promise<GroupMember[]> {
  const data = await requestJson<{ items: GroupMember[] }>(
    `/api/v1/organizations/${encodeURIComponent(organizationId)}/groups/${encodeURIComponent(groupId)}/members`,
  );
  return data.items;
}

export async function setGroupMember(
  organizationId: string,
  groupId: string,
  userId: string,
  input: Pick<GroupMember, 'role'> & { status: 'active' | 'suspended' },
): Promise<GroupMember> {
  return requestJson(
    `/api/v1/organizations/${encodeURIComponent(organizationId)}/groups/${encodeURIComponent(groupId)}/members/${encodeURIComponent(userId)}`,
    { method: 'PUT', body: JSON.stringify(input) },
  );
}

export async function removeGroupMember(
  organizationId: string,
  groupId: string,
  userId: string,
): Promise<void> {
  await requestJson<unknown>(
    `/api/v1/organizations/${encodeURIComponent(organizationId)}/groups/${encodeURIComponent(groupId)}/members/${encodeURIComponent(userId)}`,
    { method: 'DELETE' },
  );
}

export async function listServiceAccounts(
  organizationId: string,
): Promise<ServiceAccount[]> {
  const data = await requestJson<{ items: ServiceAccount[] }>(
    `/api/v1/organizations/${encodeURIComponent(organizationId)}/service-accounts`,
  );
  return data.items;
}

export async function updateServiceAccountStatus(
  organizationId: string,
  serviceAccountId: string,
  status: 'active' | 'disabled',
): Promise<ServiceAccount> {
  return requestJson(
    `/api/v1/organizations/${encodeURIComponent(organizationId)}/service-accounts/${encodeURIComponent(serviceAccountId)}`,
    { method: 'PATCH', body: JSON.stringify({ status }) },
  );
}

export async function rotateServiceAccountGeneration(
  organizationId: string,
  serviceAccountId: string,
): Promise<ServiceAccount> {
  return requestJson(
    `/api/v1/organizations/${encodeURIComponent(organizationId)}/service-accounts/${encodeURIComponent(serviceAccountId)}/rotate`,
    { method: 'POST' },
  );
}

function resourceAccessPath(kind: ShareableResourceKind, resourceId: string): string {
  const encodedId = encodeURIComponent(resourceId);
  switch (kind) {
    case 'workflow':
      return `/api/v1/workflows/${encodedId}/access`;
    case 'task':
      return `/api/v1/tasks/${encodedId}/access`;
    case 'deployment':
      return `/api/v1/deployments/${encodedId}/access`;
    case 'skill_installation':
      return `/api/v1/skills/${encodedId}/access`;
    case 'knowledge_base':
      return `/api/v1/kb/${encodedId}/access`;
  }
}

export async function listResourceBindings(
  kind: ShareableResourceKind,
  resourceId: string,
): Promise<DirectBinding[]> {
  const data = await requestJson<{ items: DirectBinding[] }>(
    resourceAccessPath(kind, resourceId),
  );
  return data.items;
}

export async function resolveResourceShareTarget(
  kind: ShareableResourceKind,
  resourceId: string,
  input: { target_type: ShareTargetType; identifier: string },
): Promise<ResolvedShareTarget | null> {
  const data = await requestJson<{ target: ResolvedShareTarget | null }>(
    `/api/v1/resource-access/${encodeURIComponent(kind)}/${encodeURIComponent(resourceId)}/resolve-target`,
    { method: 'POST', body: JSON.stringify(input) },
  );
  return data.target;
}

export async function listSharedResources(
  resourceType: ShareableResourceKind,
  limit = 30,
  offset = 0,
): Promise<{ items: SharedResource[]; next_offset: number | null }> {
  const params = new URLSearchParams({
    resource_type: resourceType,
    limit: String(limit),
    offset: String(offset),
  });
  return requestJson(`/api/v1/resource-access/shared?${params.toString()}`);
}

export async function grantResolvedResourceBinding(
  kind: ShareableResourceKind,
  resourceId: string,
  relation: ShareRelation,
  resolutionToken: string,
): Promise<DirectBinding> {
  return requestJson(resourceAccessPath(kind, resourceId), {
    method: 'POST',
    headers: { 'Idempotency-Key': crypto.randomUUID() },
    body: JSON.stringify({
      relation,
      resolution_token: resolutionToken,
    }),
  });
}

export async function revokeResourceBinding(
  kind: ShareableResourceKind,
  resourceId: string,
  binding: Omit<DirectBinding, 'source'>,
): Promise<DirectBinding> {
  return requestJson(resourceAccessPath(kind, resourceId), {
    method: 'DELETE',
    headers: { 'Idempotency-Key': crypto.randomUUID() },
    body: JSON.stringify(binding),
  });
}
