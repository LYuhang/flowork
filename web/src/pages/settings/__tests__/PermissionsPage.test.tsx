import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { PermissionsPage } from '../PermissionsPage';
import { listOrganizations } from '@/lib/api/organizations';
vi.mock('@/lib/api/organizations', () => ({ listOrganizations: vi.fn() }));
vi.mock('@/stores/auth', () => ({ useAuthStore: (select: (state: unknown) => unknown) => select({ user: { tenant_id: 'org' } }) }));
vi.mock('../OrganizationSettingsPanel', () => ({ OrganizationSettingsPanel: () => <div>Member management</div> }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
describe('Permissions page entry', () => {
  it.each(['owner', 'admin', 'member', 'guest', 'auditor', 'personal'])('gates %s', async (role) => {
    vi.mocked(listOrganizations).mockResolvedValue({ active_organization_id: 'org', session_generation: 1, items: [{ organization_id: 'org', kind: role === 'personal' ? 'personal' : 'business', role: role === 'personal' ? 'owner' : role, status: 'active' }] } as Awaited<ReturnType<typeof listOrganizations>>);
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/permissions']}><Routes><Route path="/permissions" element={<PermissionsPage />} /><Route path="/chat" element={<div>Chat home</div>} /></Routes></MemoryRouter></QueryClientProvider>);
    expect(await screen.findByText(['owner', 'admin'].includes(role) ? 'Member management' : 'Chat home')).toBeInTheDocument();
  });
});
