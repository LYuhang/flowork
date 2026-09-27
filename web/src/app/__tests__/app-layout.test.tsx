import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { useUIStore } from '@/stores/ui';

const { authState } = vi.hoisted(() => ({
  authState: {
    bootstrapped: true,
    bootstrap: () => {},
    organizationSwitching: false,
    privilegedAccess: null as null | {
      requestId: string;
      organizationId: string;
      resourceType: string | null;
      resourceId: string | null;
      actions: string[];
      expiresAt: string;
    },
    exitPrivilegedAccess: vi.fn().mockResolvedValue(undefined),
    refreshPrivilegedAccess: vi.fn().mockResolvedValue(undefined),
  },
}));

// AppLayout pulls in a lot of shell chrome we don't exercise here. Stub the
// heavy peers down to testid markers and the auth store to a no-op bootstrap.
// These modules are NOT vi.mock'd by any sibling app/__tests__ file (which
// matters under vitest isolate=false — the shared-module-graph pitfall).
vi.mock('@/app/AppSidebar', () => ({ AppSidebar: () => <div data-testid="app-sidebar" /> }));
vi.mock('@/app/KeyboardShortcuts', () => ({ KeyboardShortcuts: () => null }));
vi.mock('@/components/command-palette/CommandPalette', () => ({ CommandPalette: () => null }));
vi.mock('@/pages/canvas/explorer/VfsExplorer', () => ({
  VfsExplorer: (props: { wfId: string; readOnly: boolean }) => (
    <div data-testid="vfs-explorer" data-wfid={props.wfId} data-readonly={String(props.readOnly)} />
  ),
}));
vi.mock('@/stores/auth', () => ({
  useAuthStore: Object.assign(
    (sel: (s: typeof authState) => unknown) => sel(authState),
    { getState: () => authState },
  ),
}));

import { AppLayout } from '@/app/AppLayout';

function renderAt(path: string) {
  // A real (memory) router tree so `useMatches()` populates child-route params
  // (the production layout-route + child shape: AppLayout owns the Outlet).
  const router = createMemoryRouter(
    [
      {
        element: <AppLayout />,
        children: [
          { path: 'workspace', element: <div data-testid="workspace-page" /> },
          { path: 'tasks', element: <div data-testid="tasks-page" /> },
          { path: 'workflow/:wfId', element: <div data-testid="canvas-page" /> },
          { path: 'workflow/:wfId/version/:vKey', element: <div data-testid="canvas-page-pinned" /> },
        ],
      },
    ],
    { initialEntries: [path] },
  );
  return render(<RouterProvider router={router} />);
}

beforeEach(() => {
  useUIStore.setState({ explorerOpen: true });
  authState.organizationSwitching = false;
  authState.privilegedAccess = null;
  authState.exitPrivilegedAccess.mockClear();
});

describe('AppLayout workflow shell (route-gated)', () => {
  it.each(['/workspace', '/tasks'])('contains scrolling in the headerless shell on %s', (path) => {
    renderAt(path);
    expect(screen.queryByTestId('app-header')).not.toBeInTheDocument();
    expect(screen.getByRole('main')).toBeInTheDocument();
    expect(screen.getByTestId('mobile-app-header')).toBeInTheDocument();
    // Anchor absolute sr-only table labels inside the clipped viewport, not body.
    expect(screen.getByRole('main').closest('.surface-shell')).toHaveClass('relative', 'h-screen', 'overflow-hidden');
  });

  it('renders the Explorer on /workflow/:wfId without embedding chat', async () => {
    renderAt('/workflow/wf_42');
    const exp = await screen.findByTestId('vfs-explorer');
    expect(exp).toBeInTheDocument();
    expect(exp).toHaveAttribute('data-wfid', 'wf_42');
    expect(exp).toHaveAttribute('data-readonly', 'false');
    expect(screen.queryByTestId('agent-sidebar')).toBeNull();
  });

  it('passes readOnly=true on a pinned /version route', async () => {
    renderAt('/workflow/wf_42/version/v1.sv0');
    expect(await screen.findByTestId('vfs-explorer')).toHaveAttribute('data-readonly', 'true');
  });

  it('suppresses the Explorer on /workspace (no wfId in the route)', () => {
    renderAt('/workspace');
    expect(screen.queryByTestId('vfs-explorer')).toBeNull();
    expect(screen.queryByTestId('agent-sidebar')).toBeNull();
    expect(screen.getByTestId('workspace-page')).toBeInTheDocument();
  });

  it('hides the Explorer when explorerOpen=false on /workflow', () => {
    useUIStore.setState({ explorerOpen: false });
    renderAt('/workflow/wf_42');
    expect(screen.queryByTestId('vfs-explorer')).toBeNull();
    expect(screen.queryByTestId('agent-sidebar')).toBeNull();
  });

  it('renders the nav AppSidebar on a management route (no wfId) and NOT inside a workflow', () => {
    renderAt('/workspace');
    expect(screen.getByTestId('app-sidebar')).toBeInTheDocument();
    // Mutually exclusive with the workflow shell: no Explorer here.
    expect(screen.queryByTestId('vfs-explorer')).toBeNull();
    expect(screen.queryByTestId('agent-sidebar')).toBeNull();
  });

  it('hides the nav AppSidebar inside a workflow editor (route has wfId)', () => {
    renderAt('/workflow/wf_42');
    expect(screen.queryByTestId('app-sidebar')).toBeNull();
    expect(screen.queryByTestId('mobile-app-header')).toBeNull();
    expect(screen.queryByTestId('agent-sidebar')).toBeNull();
  });

  it('unmounts every organization-scoped surface during a Session switch', () => {
    authState.organizationSwitching = true;
    renderAt('/workspace');
    expect(screen.getByTestId('organization-switch-pending')).toBeInTheDocument();
    expect(screen.queryByTestId('workspace-page')).toBeNull();
    expect(screen.queryByTestId('app-sidebar')).toBeNull();
    expect(screen.queryByTestId('vfs-explorer')).toBeNull();
  });

  it('shows the global support scope and exposes an explicit exit', () => {
    authState.privilegedAccess = {
      requestId: 'request-1',
      organizationId: 'organization-1',
      resourceType: 'workflow',
      resourceId: 'workflow-42',
      actions: ['view', 'update'],
      expiresAt: new Date(Date.now() + 60_000).toISOString(),
    };
    renderAt('/workspace');
    expect(screen.getByTestId('privileged-support-banner')).toHaveTextContent(
      'workflow:workflow-42',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Exit support mode' }));
    expect(authState.exitPrivilegedAccess).toHaveBeenCalledOnce();
  });
});
