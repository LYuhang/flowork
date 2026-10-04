import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import i18n from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ResourceShareDialog } from '@/components/modals/ResourceShareDialog';
import en from '@/lib/i18n/locales/en.json';
import zh from '@/lib/i18n/locales/zh.json';
import {
  grantResolvedResourceBinding,
  listOrganizations,
  listOrganizationGroups,
  listResourceBindings,
  resolveResourceShareTarget,
} from '@/lib/api/organizations';
import { useAuthStore } from '@/stores/auth';

vi.mock('@/lib/api/organizations', () => ({
  grantResolvedResourceBinding: vi.fn(),
  listOrganizations: vi.fn(),
  listOrganizationGroups: vi.fn().mockResolvedValue([]),
  listResourceBindings: vi.fn(),
  resolveResourceShareTarget: vi.fn(),
  revokeResourceBinding: vi.fn(),
}));

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: {
    en: { translation: en },
    zh: { translation: zh },
  },
  interpolation: { escapeValue: false },
});

function renderDialog() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={testI18n}>
        <ResourceShareDialog
          open
          onOpenChange={vi.fn()}
          resourceKind="workflow"
          resourceId="workflow-1"
          resourceName="Release workflow"
          effectiveRole="manager"
          accessSource="computed"
        />
      </I18nextProvider>
    </QueryClientProvider>,
  );
}

describe('ResourceShareDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    void testI18n.changeLanguage('en');
    useAuthStore.setState({
      authenticated: true,
      bootstrapped: true,
      user: {
        user_id: 'owner-user',
        tenant_id: 'personal-organization',
        email: 'owner@example.com',
        displayName: 'Owner',
      },
    });
    vi.mocked(listOrganizations).mockResolvedValue({
      active_organization_id: 'personal-organization',
      session_generation: 1,
      items: [{
        organization_id: 'personal-organization',
        kind: 'personal',
        slug: 'owner-personal',
        name: 'Owner',
        membership_id: 'membership-owner',
        role: 'owner',
        status: 'active',
        active: true,
        access: {
          capabilities: ['view_metadata'],
          effective_role: 'owner',
          source: 'computed',
        },
      }],
    });
    vi.mocked(listResourceBindings).mockResolvedValue([]);
    vi.mocked(resolveResourceShareTarget).mockResolvedValue({
      target_type: 'user',
      display_name: 'Recipient',
      detail: 'r*******@example.com',
      resolution_token: 'signed-resolution-token',
      allowed_relations: ['viewer', 'editor', 'operator'],
    });
    vi.mocked(grantResolvedResourceBinding).mockResolvedValue({
      relation: 'viewer',
      subject_type: 'user',
      subject_id: 'recipient-user',
      subject_relation: null,
      source: 'direct',
    });
  });

  it('searches only after an explicit action and reveals the role after resolution', async () => {
    const user = userEvent.setup();
    renderDialog();

    const email = screen.getByLabelText('Complete email address');
    expect(screen.getAllByRole('combobox')).toHaveLength(1);
    expect(screen.queryByText('Recipient')).not.toBeInTheDocument();

    await user.type(email, 'recipient@example.com');
    expect(resolveResourceShareTarget).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Search' }));
    expect(await screen.findByText('Recipient')).toBeInTheDocument();
    expect(screen.getByText('r*******@example.com')).toBeInTheDocument();
    expect(screen.getAllByRole('combobox')).toHaveLength(2);
    expect(resolveResourceShareTarget).toHaveBeenCalledWith(
      'workflow',
      'workflow-1',
      { target_type: 'user', identifier: 'recipient@example.com' },
    );

    await user.click(screen.getByRole('button', { name: 'Add' }));
    await waitFor(() => expect(grantResolvedResourceBinding).toHaveBeenCalledWith(
      'workflow',
      'workflow-1',
      'viewer',
      'signed-resolution-token',
    ));
  });

  it('does not expose organization or department discovery for personal resources', async () => {
    renderDialog();
    expect(await screen.findByText('Search another account by its complete email address.')).toBeInTheDocument();
    expect(screen.queryByText('Department/Team')).not.toBeInTheDocument();
    expect(screen.queryByText('Entire organization')).not.toBeInTheDocument();
    expect(screen.queryByText(/Filter users or groups/i)).not.toBeInTheDocument();
  });

  it('does not expose an internal subject ID when presentation data is unavailable', async () => {
    vi.mocked(listResourceBindings).mockResolvedValue([{
      relation: 'viewer',
      subject_type: 'user',
      subject_id: 'internal-recipient-123456',
      subject_relation: null,
      source: 'direct',
      display_name: '',
      detail: '',
    }]);

    renderDialog();
    expect(await screen.findByText('Viewer')).toBeInTheDocument();
    expect(screen.getAllByText('User').length).toBeGreaterThan(0);
    expect(screen.queryByText(/123456/)).not.toBeInTheDocument();
  });

  it('renders the exact-search flow in Simplified Chinese', async () => {
    await testI18n.changeLanguage('zh');
    renderDialog();

    expect(screen.getByRole('heading', { name: '分享资源' })).toBeInTheDocument();
    expect(screen.getByText('管理员 · 系统计算')).toBeInTheDocument();
    expect(screen.queryByText('manager · computed')).not.toBeInTheDocument();
    expect(screen.getByLabelText('完整邮箱地址')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '搜索' })).toBeInTheDocument();
  });
  it('filters authorized departments and resolves the selected full path', async () => {
    const user = userEvent.setup();
    const organizations = await listOrganizations();
    vi.mocked(listOrganizations).mockResolvedValue({ ...organizations, items: organizations.items.map((item) => ({ ...item, kind: 'business' })) });
    const base = { organization_id: 'personal-organization', kind: 'department' as const, source: 'native' as const, directory_provider_id: null, external_id: null, status: 'active' as const, created_by: 'owner-user', created_at: '', updated_at: '', access: { capabilities: [], effective_role: null, source: 'computed' } };
    vi.mocked(listOrganizationGroups).mockResolvedValue([
      { ...base, group_id: 'parent', parent_group_id: null, name: 'Engineering' },
      { ...base, group_id: 'child', parent_group_id: 'parent', name: 'Platform' },
    ]);
    renderDialog();
    await user.click(await screen.findByRole('combobox', { name: 'Target type' }));
    await user.click(await screen.findByRole('option', { name: 'Department/Team' }));
    await user.type(screen.getByLabelText('Department or team name'), 'Plat');
    await user.click(await screen.findByRole('button', { name: 'Engineering / Platform' }));
    await waitFor(() => expect(resolveResourceShareTarget).toHaveBeenCalledWith('workflow', 'workflow-1', { target_type: 'group', identifier: 'Engineering / Platform' }));
  });

});
