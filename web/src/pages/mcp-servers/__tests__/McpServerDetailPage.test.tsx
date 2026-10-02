/**
 * MCP SP1-T8 — `McpServerDetailPage` smoke test.
 *
 * Mocks the `useMcpServer` hook (so the page renders against a stable
 * fixture without a network round-trip) and mounts the page under a
 * `:id`-param route via `MemoryRouter` (mirrors `DeploymentDetailPage`).
 * Asserts:
 *   - the server name renders (heading)
 *   - transport + endpoint render
 *   - the probed tool `srch__query` renders
 *   - a Back link to `/mcp-servers` exists
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import i18n from 'i18next';
import { TooltipProvider } from '@/components/ui/tooltip';

const SERVER_ID = 'mcp-1';

vi.mock('@/lib/api/queries/mcp-servers', () => ({
  useMcpServer: vi.fn(() => ({
    data: {
      id: SERVER_ID,
      name: 'Search tools',
      tool_prefix: 'srch',
      transport: 'sse',
      endpoint: 'https://search.example.com/sse',
      auth_mode: 'configuration',
      connection_status: 'not_required',
      description: 'Search the web and return relevant results.',
      description_source: 'synthesized',
      description_model_id: null,
      description_generated_at: null,
      description_basis_hash: null,
      auth_config: { type: 'bearer', token: '***' },
      enabled: true,
      last_handshake_status: 'ok',
      last_tool_count: 1,
      last_tool_names: [{
        name: 'srch__query',
        description: 'Search',
        input_schema: {
          type: 'object',
          properties: { query: { type: 'string', description: 'Search query' } },
          required: ['query'],
        },
      }],
      last_handshake_at: '2026-06-01T00:00:00Z',
      created_at: '2026-06-01T00:00:00Z',
      updated_at: '2026-06-02T00:00:00Z',
    },
    isLoading: false,
    isError: false,
  })),
  useUpdateMcpServer: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useRefreshMcpServer: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useDeleteMcpServer: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useStartMcpOAuth: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useDisconnectMcpOAuth: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
}));

import { McpServerDetailPage } from '@/pages/mcp-servers/McpServerDetailPage';

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: {} } },
  interpolation: { escapeValue: false },
});

function renderAt(id: string) {
  return render(
    <I18nextProvider i18n={testI18n}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[`/mcp-servers/${id}`]}>
          <Routes>
            <Route
              path="/mcp-servers/:id"
              element={<McpServerDetailPage />}
            />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </I18nextProvider>,
  );
}

describe('<McpServerDetailPage>', () => {
  it('renders name, transport/endpoint, probed tool, and a Back link', async () => {
    const user = userEvent.setup();
    renderAt(SERVER_ID);

    // Heading — the server name.
    expect(
      screen.getByRole('heading', { name: 'Search tools' }),
    ).toBeInTheDocument();

    // Transport + endpoint surface on the page (transport appears in both
    // the sub-header chip and the config grid → assert ≥1).
    expect(screen.getAllByText('sse').length).toBeGreaterThan(0);
    await user.click(screen.getByRole('tab', { name: /connection & settings/i }));
    expect(
      screen.getByText('https://search.example.com/sse'),
    ).toBeInTheDocument();

    // The probed tool name renders.
    await user.click(screen.getByRole('tab', { name: /tools/i }));
    expect(screen.getAllByText('srch__query')).toHaveLength(2);
    expect(screen.getByText('Search query')).toBeInTheDocument();

    // A Back link pointing at the list route.
    const back = screen.getByRole('link', { name: /back/i });
    expect(back).toHaveAttribute('href', '/mcp-servers');
    // Sanity: the link is the one we expect.
    expect(within(back).queryByText(/back/i) ?? back).toBeInTheDocument();
  });
});
