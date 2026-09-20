/**
 * Integration test for `<WorkspacePage>`.
 *
 * The full happy path through TanStack Query → openapi-fetch → MSW. The
 * point is to prove the test plumbing works end-to-end, not to exhaustively
 * cover every UI state. Three scenarios:
 *
 *   1. The page mounts under the Provider stack and renders its header.
 *   2. The empty-state copy renders when MSW returns an empty page.
 *   3. A workflow row renders in the table when MSW returns one item.
 *
 * We wrap the page in a fresh `QueryClient` per test to keep cache state
 * isolated (the production singleton would carry results from prior tests).
 * `MemoryRouter` is required because `CreateWorkflowDialog` (mounted by
 * WorkspacePage even when closed) calls `useNavigate()` at render time.
 *
 * We do NOT mount the full router or auth dialog — the page is the unit
 * under test, and dragging in TokenDialog/AppLayout would test scaffolding
 * we already cover in E2E.
 */
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import i18n from 'i18next';
import { MemoryRouter } from 'react-router';
import { WorkspacePage } from '@/pages/workspace/WorkspacePage';
import { TooltipProvider } from '@/components/ui/tooltip';
import { server, fixtureWorkflow } from '@/__tests__/msw-handlers';

// Minimal local i18n instance — we don't want the test to depend on the
// main `@/lib/i18n` bundle (which mutates localStorage and pulls the full
// locale JSON). The defaultValue fallbacks in `t('key', 'default')` make
// this safe: an empty resources map just falls through to the literal.
const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: {} } },
  interpolation: { escapeValue: false },
});

function renderWithProviders(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={testI18n}>
        <MemoryRouter><TooltipProvider>{ui}</TooltipProvider></MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  );
}

describe('<WorkspacePage>', () => {
  it('renders the page header under the Provider stack', async () => {
    renderWithProviders(<WorkspacePage />);
    expect(await screen.findByText('Workflows')).toBeInTheDocument();
  });

  it('keeps the management table and a single create action when empty', async () => {
    renderWithProviders(<WorkspacePage />);
    expect(await screen.findByTestId('wf-empty')).toHaveTextContent('No workflows yet');
    expect(screen.getByTestId('wf-table')).toBeInTheDocument();
    expect(screen.getByTestId('wf-search')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'New Workflow' })).toHaveLength(1);
    expect(screen.queryByText('Make repeat work a workflow.')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Describe your task' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'New Workflow' }));
    expect(await screen.findByRole('dialog', { name: 'New workflow' })).toBeInTheDocument();
  });

  it('renders a workflow row in the table when the API returns items', async () => {
    server.use(
      http.get('*/api/v1/workflows', () =>
        HttpResponse.json({
          items: [fixtureWorkflow({ wf_id: 'wf_a', workflow_name: 'Alpha' })],
          total: 1,
          limit: 50,
          offset: 0,
        }),
      ),
    );

    renderWithProviders(<WorkspacePage />);
    expect(await screen.findByText('Alpha')).toBeInTheDocument();
    expect(screen.getByTestId('wf-table')).toBeInTheDocument();
    expect(screen.getByTestId('wf-row')).toHaveAttribute('data-wf-id', 'wf_a');
  });
});
