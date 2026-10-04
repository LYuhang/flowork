/**
 * SP2-T6 — `SkillsPage` smoke tests.
 *
 * Spies on the REAL API client module (`@/lib/api/skills`) so the REAL
 * react-query hooks run end-to-end (mirrors the McpServersPage harness). We
 * use `vi.spyOn` rather than `vi.mock` because under `isolate: false` (shared
 * module graph) a `vi.mock` of `@/lib/api/skills` is order-fragile — a sibling
 * file that imports the REAL module shifts module-eval ordering and the
 * replacement silently fails to apply (see feedback_vitest_isolate_false).
 * A spy never swaps module identity, so it survives any sibling load order.
 *
 * Asserts:
 *   - one card per installed skill from a mocked list
 *   - the search box narrows to the matching skill by name
 *   - every installed skill has an uninstall action
 *   - uninstall through the menu + confirm calls the delete mutation
 *   - Custom accepts ZIP upload only (no inline new-Skill editor)
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import i18n from 'i18next';

import * as skillsClient from '@/lib/api/skills';

// jsdom lacks the Pointer Capture API + scrollIntoView that Radix Select
// calls on open. Polyfill them (documented Radix+jsdom workaround) so the
// filter dropdown can be driven by userEvent. Guarded so it's idempotent
// under the shared (isolate:false) module graph.
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => {};
  Element.prototype.releasePointerCapture = () => {};
}
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

const SKILLS = [
  {
    id: 'skill-workflow', installed: true, created_by_me: true, source: 'custom',
    name: 'Workflow Builder',
    description: 'Builds workflows from a doc.',
    allowed_tools: ['read_file', 'apply_workflow_edit'],
    version: 1,
    access: {
      capabilities: ['view', 'use', 'update', 'delete', 'publish'],
      effective_role: 'manager',
      source: 'computed',
    },
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-02T00:00:00Z',
  },
  {
    id: 'skill-tenant', installed: false, created_by_me: false, source: 'custom',
    name: 'Invoice Parser',
    description: 'Extracts totals from invoices.',
    allowed_tools: ['read_file'],
    version: 3,
    access: {
      capabilities: ['view', 'use', 'update', 'delete', 'publish'],
      effective_role: 'manager',
      source: 'computed',
    },
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-02T00:00:00Z',
  },
];

const CATALOG = Array.from({ length: 20 }, (_, index) => ({
  source: 'openai',
  source_label: 'OpenAI Skills',
  source_id: `skill-${index + 1}`,
  name: `Catalog Skill ${index + 1}`,
  description: `Catalog skill ${index + 1}`,
  version: 1,
  allowed_tools: [],
  homepage: 'https://example.test/skill',
  revision: 'abc123',
  files: [{ path: 'SKILL.md', size_bytes: 100 }],
}));

// sonner toast is a side effect we don't assert on here. Spy (not vi.mock) for
// the same isolate=false order-stability reason as the API client above.
import * as sonner from 'sonner';

import { SkillsPage } from '@/pages/skills/SkillsPage';

void i18n.use(initReactI18next).init({
  lng: 'en',
  resources: { en: { translation: {} } },
  interpolation: { escapeValue: false },
});

function renderPage(initialEntry = '/') {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <I18nextProvider i18n={i18n}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[initialEntry]}>
          <SkillsPage />
        </MemoryRouter>
      </QueryClientProvider>
    </I18nextProvider>,
  );
}

describe('SkillsPage', () => {
  let deleteSpy: ReturnType<typeof vi.spyOn>;
  let catalogSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(skillsClient, 'listSkills').mockResolvedValue(SKILLS as never);
    catalogSpy = vi.spyOn(skillsClient, 'searchSkillCatalog').mockImplementation(async (source, _search, limit) => ({
      source,
      source_label: source === 'openai' ? 'OpenAI Skills' : 'Anthropic Skills',
      revision: 'abc123',
      items: CATALOG.slice(0, limit ?? CATALOG.length),
      has_more: (limit ?? CATALOG.length) < CATALOG.length,
    }) as never);
    deleteSpy = vi
      .spyOn(skillsClient, 'deleteSkill')
      .mockResolvedValue(undefined as never);
    vi.spyOn(sonner.toast, 'success').mockImplementation(() => '' as never);
    vi.spyOn(sonner.toast, 'error').mockImplementation(() => '' as never);
  });

  it('combines relationship, installation and content source filters', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Workflow Builder');
    await user.click(screen.getByRole('combobox', { name: 'Resource relationship' }));
    await user.click(screen.getByRole('option', { name: 'Shared with me' }));
    await user.click(screen.getByRole('combobox', { name: 'Installation status' }));
    await user.click(screen.getByRole('option', { name: 'Not installed' }));
    await user.click(screen.getByRole('combobox', { name: 'Filter by skill source' }));
    await user.click(screen.getByRole('option', { name: 'Custom' }));
    expect(screen.getByText('Invoice Parser')).toBeInTheDocument();
    expect(screen.queryByText('Workflow Builder')).not.toBeInTheDocument();
    await user.click(screen.getByRole('combobox', { name: 'Installation status' }));
    await user.click(screen.getByRole('option', { name: /^Installed$/ }));
    expect(screen.queryByTestId('skill-card')).not.toBeInTheDocument();
  });

  it('installs a shared Skill without deleting the package', async () => {
    const install = vi.spyOn(skillsClient, 'setSkillInstalled').mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Invoice Parser');
    await user.click(screen.getByRole('button', { name: /^Install$/ }));
    await waitFor(() => expect(install).toHaveBeenCalledWith('skill-tenant', true));
    expect(deleteSpy).not.toHaveBeenCalled();
  });

  it('renders a card per skill', async () => {
    renderPage();
    await waitFor(() =>
      expect(screen.getByText('Workflow Builder')).toBeInTheDocument(),
    );
    expect(screen.getByText('Invoice Parser')).toBeInTheDocument();
    expect(screen.getAllByTestId('skill-card')).toHaveLength(2);
  });

  it('narrows by name when typing in the search box', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() =>
      expect(screen.getByText('Workflow Builder')).toBeInTheDocument(),
    );

    await user.type(screen.getByTestId('skill-search'), 'invoice');

    await waitFor(() =>
      expect(screen.queryByText('Workflow Builder')).not.toBeInTheDocument(),
    );
    expect(screen.getByText('Invoice Parser')).toBeInTheDocument();
    expect(screen.getAllByTestId('skill-card')).toHaveLength(1);
  });

  it('every installed skill has an actions menu', async () => {
    renderPage();
    await waitFor(() =>
      expect(screen.getByText('Workflow Builder')).toBeInTheDocument(),
    );

    const cards = screen.getAllByTestId('skill-card');
    const workflowCard = cards.find((c) =>
      within(c).queryByText('Workflow Builder'),
    )!;
    const invoiceCard = cards.find((c) =>
      within(c).queryByText('Invoice Parser'),
    )!;

    expect(within(workflowCard).getByRole('button', { name: 'Open actions for Workflow Builder' })).toBeInTheDocument();
    expect(within(invoiceCard).getByRole('button', { name: 'Open actions for Invoice Parser' })).toBeInTheDocument();
  });

  it('does not expose sharing for private Skill installations', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText('Workflow Builder')).toBeInTheDocument());

    const workflowCard = screen.getAllByTestId('skill-card').find((card) =>
      within(card).queryByText('Workflow Builder'),
    )!;
    await user.click(within(workflowCard).getByRole('button', { name: 'Open actions for Workflow Builder' }));

    expect(screen.queryByRole('menuitem', { name: 'Share Skill' })).not.toBeInTheDocument();
  });

  it('offers ZIP-only Custom Skill import without an inline creator', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() =>
      expect(screen.getAllByRole('button', { name: /upload skill package/i })).toHaveLength(1),
    );

    await user.click(screen.getAllByRole('button', { name: /upload skill package/i })[0]);
    expect(screen.queryByRole('textbox', { name: /skill\\.md/i })).not.toBeInTheDocument();
    expect(document.querySelector('input[type="file"][accept*=".zip"]')).toBeInTheDocument();
  });

  it('deletes a tenant skill through its menu + confirm', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() =>
      expect(screen.getByText('Invoice Parser')).toBeInTheDocument(),
    );

    const cards = screen.getAllByTestId('skill-card');
    const tenantCard = cards.find((c) =>
      within(c).queryByText('Invoice Parser'),
    )!;

    await user.click(within(tenantCard).getByTestId('skill-card-menu'));
    await user.click(await screen.findByTestId('skill-menu-delete'));
    await user.click(await screen.findByTestId('skill-confirm-delete'));

    await waitFor(() =>
      expect(deleteSpy).toHaveBeenCalledWith(
        'skill-tenant',
        expect.any(Object),
      ),
    );
  });

  it('shows ten catalog entries initially and appends ten more on demand', async () => {
    const user = userEvent.setup();
    renderPage('/skills?tab=discover');

    await waitFor(() => expect(screen.getAllByTestId('skill-catalog-card')).toHaveLength(10));
    await user.click(screen.getByTestId('skill-catalog-more'));

    await waitFor(() => expect(screen.getAllByTestId('skill-catalog-card')).toHaveLength(20));
  });

  it('searches only on Search or Enter and shows install guidance before results', async () => {
    const user = userEvent.setup();
    renderPage('/skills?tab=discover');

    await waitFor(() => expect(screen.getAllByTestId('skill-catalog-card')).toHaveLength(10));
    expect(catalogSpy).toHaveBeenCalledTimes(1);

    const input = screen.getByTestId('skill-catalog-search');
    await user.type(input, 'spreadsheets');
    expect(catalogSpy).toHaveBeenCalledTimes(1);

    await user.click(screen.getByTestId('skill-catalog-search-button'));
    await waitFor(() => expect(catalogSpy).toHaveBeenLastCalledWith('openai', 'spreadsheets', 10));

    const guideline = screen.getByText('Install Guideline');
    const firstCard = screen.getAllByTestId('skill-catalog-card')[0];
    expect(guideline.compareDocumentPosition(firstCard) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    await user.clear(input);
    await user.type(input, 'documents{Enter}');
    await waitFor(() => expect(catalogSpy).toHaveBeenLastCalledWith('openai', 'documents', 10));
  });

  it('keeps Search available while the initial catalog request is still loading', async () => {
    catalogSpy.mockImplementation(() => new Promise(() => {}) as never);
    const user = userEvent.setup();
    renderPage('/skills?tab=discover');

    const button = await screen.findByTestId('skill-catalog-search-button');
    expect(button).toBeEnabled();
    await user.type(screen.getByTestId('skill-catalog-search'), 'documents');
    await user.click(button);

    await waitFor(() => expect(catalogSpy).toHaveBeenLastCalledWith('openai', 'documents', 10));
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent('Searching');
  });
});
