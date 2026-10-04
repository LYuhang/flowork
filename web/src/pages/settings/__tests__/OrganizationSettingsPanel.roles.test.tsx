import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import i18n from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { OrganizationSettingsPanel } from '@/pages/settings/OrganizationSettingsPanel';
import { useAuthStore } from '@/stores/auth';
import {
  rotateServiceAccountGeneration,
  updateOrganizationMember,
} from '@/lib/api/organizations';

const roleState = vi.hoisted(() => ({
  role: 'member' as 'owner' | 'admin' | 'member' | 'guest' | 'auditor',
  capabilities: [] as string[],
}));

vi.mock('@/lib/api/organizations', () => ({
  listOrganizations: vi.fn(async () => ({
    active_organization_id: 'organization-roles',
    session_generation: 1,
    items: [{
      organization_id: 'organization-roles',
      kind: 'business',
      slug: 'roles',
      name: 'Role Matrix Company',
      membership_id: 'membership-self',
      role: roleState.role,
      status: 'active',
      active: true,
      access: {
        capabilities: roleState.capabilities,
        effective_role: roleState.role,
        source: 'direct',
      },
    }],
  })),
  getOrganizationSelf: vi.fn(async () => ({
    membership: {
      membership_id: 'membership-self',
      user_id: 'role-user',
      email: 'role-user@example.com',
      display_name: 'Role User',
      role: roleState.role,
      status: 'active',
      source: 'native',
      directory_provider_id: null,
      created_at: '2026-08-04T00:00:00Z',
      updated_at: '2026-08-04T00:00:00Z',
    },
    groups: [{
      group_id: 'group-one',
      name: 'Engineering',
      kind: 'department',
      role: 'member',
      source: 'native',
    }],
  })),
  listOrganizationMembers: vi.fn(async () => [{
    membership_id: 'membership-other',
    user_id: 'other-user',
    email: 'other@example.com',
    display_name: 'Other User',
    role: 'member',
    status: 'active',
    source: 'native',
    directory_provider_id: null,
    created_at: '2026-08-04T00:00:00Z',
    updated_at: '2026-08-04T00:00:00Z',
  }]),
  listOrganizationGroups: vi.fn(async () => [{
    group_id: 'group-one',
    organization_id: 'organization-roles',
    parent_group_id: null,
    kind: 'department',
    name: 'Engineering',
    source: 'native',
    directory_provider_id: null,
    external_id: null,
    status: 'active',
    created_by: 'role-user',
    created_at: '2026-08-04T00:00:00Z',
    updated_at: '2026-08-04T00:00:00Z',
    access: {
      capabilities: roleState.capabilities.includes('manage_members')
        ? ['view_metadata', 'manage_members']
        : ['view_metadata'],
      effective_role: roleState.role,
      source: 'direct',
    },
  }]),
  listGroupMembers: vi.fn(async () => []),
  listServiceAccounts: vi.fn(async () => [{
    service_account_id: 'service-account-one',
    organization_id: 'organization-roles',
    name: 'Scheduled workflow runner',
    kind: 'task',
    owner_resource_type: 'task',
    owner_resource_id: 'redacted-resource',
    status: 'active',
    generation: 1,
    created_at: '2026-08-04T00:00:00Z',
    updated_at: '2026-08-04T00:00:00Z',
  }]),
  createOrganizationGroup: vi.fn(),
  removeGroupMember: vi.fn(),
  rotateServiceAccountGeneration: vi.fn(),
  setGroupMember: vi.fn(),
  updateOrganizationMember: vi.fn(),
  updateServiceAccountStatus: vi.fn(),
}));

vi.mock('@/lib/api/enterprise-identity', () => ({
  listEnterpriseIdentityProviders: vi.fn(async () => []),
  createEnterpriseIdentityProvider: vi.fn(),
  rotateEnterpriseScimToken: vi.fn(),
  setEnterpriseIdentityProviderStatus: vi.fn(),
  updateEnterpriseOidcClientAuth: vi.fn(),
}));

vi.mock('@/lib/api/audit', () => ({
  listAudit: vi.fn(async () => ({ items: [], next_cursor: null })),
}));

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: {} } },
  interpolation: { escapeValue: false },
});

function renderRole(
  role: typeof roleState.role,
  capabilities: string[],
) {
  roleState.role = role;
  roleState.capabilities = capabilities;
  useAuthStore.setState({
    authenticated: true,
    bootstrapped: true,
    user: {
      user_id: 'role-user',
      tenant_id: 'organization-roles',
      email: 'role-user@example.com',
      displayName: 'Role User',
    },
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={testI18n}>
        <OrganizationSettingsPanel />
      </I18nextProvider>
    </QueryClientProvider>,
  );
}

describe('organization role surface', () => {
  beforeEach(() => {
    roleState.role = 'member';
    roleState.capabilities = [];
    vi.mocked(updateOrganizationMember).mockReset();
    vi.mocked(updateOrganizationMember).mockResolvedValue({} as never);
    vi.mocked(rotateServiceAccountGeneration).mockReset();
    vi.mocked(rotateServiceAccountGeneration).mockResolvedValue({} as never);
  });

  for (const role of ['member', 'guest'] as const) {
    it(`${role} sees only self and assigned-team information`, async () => {
      renderRole(role, ['view_metadata']);
      expect(await screen.findByText('Role Matrix Company')).toBeInTheDocument();
      expect(screen.getByRole('tablist', { name: 'Organization sections' })).toBeInTheDocument();
      expect(screen.getByRole('tab', { name: /my teams/i })).toBeInTheDocument();
      expect(screen.queryByRole('tab', { name: /^people$/i })).toBeNull();
      expect(screen.queryByRole('tab', { name: /security & identity/i })).toBeNull();
      expect(screen.queryByRole('tab', { name: /operations/i })).toBeNull();
    });
  }

  it('admin can manage people, departments, and identity policy', async () => {
    const user = userEvent.setup();
    renderRole('admin', ['view_audit', 'manage_members', 'manage_policy']);

    await user.click(await screen.findByRole('tab', { name: /^people$/i }));
    expect(await screen.findAllByRole('combobox')).toHaveLength(2);

    await user.click(screen.getByRole('tab', { name: /departments & teams/i }));
    expect(screen.getByRole('button', { name: /new group/i })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /^add member$/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /security & identity/i })).toBeInTheDocument();
  });

  it('auditor can inspect directory and operations without mutation controls', async () => {
    const user = userEvent.setup();
    renderRole('auditor', ['view_audit']);

    await user.click(await screen.findByRole('tab', { name: /^people$/i }));
    expect(await screen.findByText('Read-only organization membership directory.')).toBeInTheDocument();
    expect(screen.queryByRole('combobox')).toBeNull();

    await user.click(screen.getByRole('tab', { name: /departments & teams/i }));
    expect(screen.queryByRole('button', { name: /new group/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /^add member$/i })).toBeNull();

    await user.click(screen.getByRole('tab', { name: /operations/i }));
    expect(await screen.findByText('Scheduled workflow runner')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /rotate|disable/i })).toBeNull();
    expect(screen.queryByRole('tab', { name: /security & identity/i })).toBeNull();
  });

  it('shows row-local member save feedback', async () => {
    const user = userEvent.setup();
    renderRole('admin', ['view_audit', 'manage_members', 'manage_policy']);
    await user.click(await screen.findByRole('tab', { name: /^people$/i }));
    await user.click((await screen.findAllByRole('combobox'))[0]);
    expect(screen.queryByRole('option', { name: 'admin' })).toBeNull();
    expect(screen.queryByRole('option', { name: 'owner' })).toBeNull();
    await user.click(screen.getByRole('option', { name: 'guest' }));
    expect(await screen.findByText('Saved')).toBeInTheDocument();
    expect(updateOrganizationMember).toHaveBeenCalledWith(
      'organization-roles',
      'other-user',
      { role: 'guest', status: 'active' },
    );
  });

  it('confirms service-account rotation before making a request', async () => {
    const user = userEvent.setup();
    renderRole('admin', ['view_audit', 'manage_members', 'manage_policy']);
    await user.click(await screen.findByRole('tab', { name: /operations/i }));
    await user.click(await screen.findByRole('button', { name: /^Rotate$/i }));
    expect(screen.getByText(/Existing credentials.*will stop working/i)).toBeInTheDocument();
    expect(rotateServiceAccountGeneration).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: /^Cancel$/i }));
    expect(rotateServiceAccountGeneration).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: /^Rotate$/i }));
    await user.click(screen.getAllByRole('button', { name: /^Rotate$/i }).at(-1)!);
    await waitFor(() => expect(rotateServiceAccountGeneration).toHaveBeenCalledWith(
      'organization-roles',
      'service-account-one',
    ));
  });
});
