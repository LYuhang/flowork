import { useSkill } from '@/lib/api/queries/skills';
/**
 * SP2-T6 — `SkillDetailPage` smoke test.
 *
 * Mocks the `useSkill` hook (so the page renders against a stable fixture
 * without a network round-trip) and mounts the page under a `:id`-param route
 * via `MemoryRouter` (mirrors `McpServerDetailPage`).
 * Asserts:
 *   - the skill name renders (heading)
 *   - the SKILL.md `body` renders
 *   - the `allowed_tools` chips render
 *   - a Back link to `/skills` exists
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import i18n from 'i18next';

const SKILL_ID = 'skill-1';
const mutations = vi.hoisted(() => ({
  saveDraft: vi.fn(),
  publish: vi.fn(),
}));

vi.mock('@/lib/api/queries/skills', () => ({
  useSkill: vi.fn(() => ({
    data: {
      id: SKILL_ID,
      name: 'Invoice Parser',
      description: 'Extracts totals from invoices.',
      allowed_tools: ['read_file', 'write_file'],
      version: 2,
      source: 'custom',
      created_at: '2026-06-01T00:00:00Z',
      updated_at: '2026-06-02T00:00:00Z',
      body: '# Invoice Parser\n\nRead the invoice and return the grand total.',
      skill_md: '---\nname: invoice-parser\ndescription: Extract totals\n---\n\n# Invoice Parser\n\nRead the invoice and return the grand total.',
      files: ['scripts/parse.py'],
      has_draft: true,
      draft_updated_at: '2026-06-03T00:00:00Z',
      access: {
        capabilities: ['view', 'use', 'update', 'delete', 'publish'],
        effective_role: 'manager',
        source: 'computed',
      },
    },
    refetch: vi.fn(async () => ({})),
    isLoading: false,
    isError: false,
  })),
  useSkillDraft: vi.fn(() => ({
    data: {
      skill_id: SKILL_ID,
      base_revision_hash: 'a'.repeat(64),
      draft_hash: 'b'.repeat(64),
      skill_md: '---\nname: invoice-parser\ndescription: Extract totals\nversion: 2\n---\n\n# Draft instructions',
      body: '# Draft instructions',
      files: ['SKILL.md', 'scripts/parse.py'],
      has_changes: true,
      updated_at: '2026-06-03T00:00:00Z',
    },
    refetch: vi.fn(async () => ({})),
    isLoading: false,
  })),
  useSkillVersions: vi.fn(() => ({
    data: [
      {
        revision_id: 'revision-latest',
        revision_hash: 'a'.repeat(64),
        version: 2,
        is_latest: true,
        files: ['SKILL.md', 'scripts/parse.py'],
        size_bytes: 100,
        created_at: '2026-06-02T00:00:00Z',
      },
      {
        revision_id: 'revision-v1',
        revision_hash: 'c'.repeat(64),
        version: 1,
        is_latest: false,
        files: ['SKILL.md'],
        size_bytes: 80,
        created_at: '2026-06-01T00:00:00Z',
      },
    ],
  })),
  useSkillVersion: vi.fn((_id: string, revisionId?: string) => ({
    data: revisionId === 'revision-v1' ? {
      revision_id: 'revision-v1',
      revision_hash: 'c'.repeat(64),
      version: 1,
      is_latest: false,
      files: ['SKILL.md'],
      size_bytes: 80,
      created_at: '2026-06-01T00:00:00Z',
      name: 'Invoice Parser v1',
      description: 'Historical parser instructions.',
      allowed_tools: ['read_file'],
      skill_md: '---\nname: invoice-parser\ndescription: Historical parser instructions.\nversion: 1\n---\n\n# Historical instructions',
      body: '# Historical instructions',
    } : undefined,
    refetch: vi.fn(async () => ({})),
    isLoading: false,
  })),
  useSetSkillInstalled: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useDeleteSkill: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useSaveSkillDraft: vi.fn(() => ({ mutateAsync: mutations.saveDraft, isPending: false })),
  usePublishSkillVersion: vi.fn(() => ({ mutateAsync: mutations.publish, isPending: false })),
}));

import en from '@/lib/i18n/locales/en.json';
import { getSkillDraftFile, writeSkillDraftFile } from '@/lib/api/skills';
vi.mock('@/lib/api/skills', () => ({getSkillFile:vi.fn(), getSkillVersionFile:vi.fn(), getSkillDraftFile:vi.fn(), writeSkillDraftFile:vi.fn()}));
import { TooltipProvider } from '@/components/ui/tooltip';
import { SkillDetailPage } from '@/pages/skills/SkillDetailPage';

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

function renderAt(id: string, suffix = '') {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  const router = createMemoryRouter(
    [
      { path: '/skills/:id', element: <SkillDetailPage /> },
      { path: '/skills', element: <div>Skill list</div> },
    ],
    { initialEntries: [`/skills/${id}${suffix}`] },
  );
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={testI18n}>
        <TooltipProvider><RouterProvider router={router} /></TooltipProvider>
      </I18nextProvider>
    </QueryClientProvider>,
  );
}

describe('<SkillDetailPage>', () => {
  beforeEach(() => {
    vi.mocked(getSkillDraftFile).mockResolvedValue({size:25, arrayBuffer:async () => new TextEncoder().encode('# Draft instructions').buffer} as Blob);
    vi.mocked(writeSkillDraftFile).mockReset().mockResolvedValue({} as never);
    mutations.saveDraft.mockReset().mockResolvedValue({});
    mutations.publish.mockReset().mockResolvedValue({});
  });

  it('offers sharing for an authorized custom Skill manager', async () => {
    const result = vi.mocked(useSkill)(SKILL_ID);
    vi.mocked(useSkill).mockReturnValueOnce({
      ...result,
      data: { ...result.data!, access: { ...result.data!.access, capabilities: ['view', 'manage_access'] } },
    } as ReturnType<typeof useSkill>);
    renderAt(SKILL_ID);
    expect(await screen.findByRole('button', { name: 'Share' })).toBeInTheDocument();
  });

  it('renders name, SKILL.md body, allowed-tools chips, and a Back link', async () => {
    const user = userEvent.setup();
    renderAt(SKILL_ID);

    // Heading — the skill name.
    expect(
      screen.getAllByRole('heading', { name: 'Invoice Parser', level:1 })[0],
    ).toBeInTheDocument();

    // The SKILL.md body surfaces in the Instructions tab.
    await user.click(screen.getByRole('tab', { name: /^files$/i }));
    expect(
      screen.getByText(/Read the invoice and return the grand total\./),
    ).toBeInTheDocument();

    // The allowed-tools chips render.
    await user.click(screen.getByRole('tab', { name: /^overview$/i }));
    expect(screen.getByText('read_file, write_file')).toBeInTheDocument();

    // A Back link pointing at the list route.
    const back = screen.getByRole('link', { name: /back/i });
    expect(back).toHaveAttribute('href', '/skills');
    expect(screen.queryByRole('button', { name: 'Share Skill' })).not.toBeInTheDocument();
  });

  it('keeps Custom Skills read-only until Edit, then saves a draft before publishing', async () => {
    const user = userEvent.setup();
    renderAt(SKILL_ID);

    expect(screen.queryByRole('textbox', { name: 'SKILL.md' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^edit$/i }));
    await user.click(screen.getByRole('button', {name:'File actions'}));
    await user.click(screen.getByRole('menuitem', {name:'Edit content'}));
    const editor = screen.getByRole('textbox', { name: 'Content' });
    await waitFor(() => expect(editor).toHaveValue('# Draft instructions'));
    expect((editor as HTMLTextAreaElement).value).toContain('# Draft instructions');

    await user.type(editor, '\n\nAdded rule.');
    await user.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(writeSkillDraftFile).toHaveBeenCalledWith(SKILL_ID, 'SKILL.md', 'b'.repeat(64), expect.any(File), false));
    expect(mutations.publish).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: /new version/i }));
    const versionInput = screen.getByLabelText(/^version$/i);
    expect(versionInput).toHaveValue(3);
    await user.click(screen.getByRole('button', { name: /^publish$/i }));
    await waitFor(() => expect(mutations.publish).toHaveBeenCalledWith({
      id: SKILL_ID,
      version: 3,
      expectedHash: 'b'.repeat(64),
    }));
  });

  it('protects unsaved file edits when closing the editor', async () => {
    const user = userEvent.setup();
    renderAt(SKILL_ID);
    await user.click(screen.getByRole('button', {name:/^edit$/i}));
    await user.click(screen.getByRole('button', {name:'File actions'}));
    await user.click(screen.getByRole('menuitem', {name:'Edit content'}));
    const editor = screen.getByRole('textbox', {name:'Content'});
    await waitFor(() => expect(editor).toHaveValue('# Draft instructions'));
    await user.type(editor, ' Unsaved rule.');
    await user.click(screen.getByRole('button', {name:/^Cancel$/}));
    expect(screen.getByText('Your unsaved changes will be lost.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', {name:'Keep editing'}));
    expect(editor).toHaveValue('# Draft instructions Unsaved rule.');
    expect(writeSkillDraftFile).not.toHaveBeenCalled();
  });

  it('renders a selected historical version as a read-only snapshot', async () => {
    renderAt(SKILL_ID, '?tab=instructions&revision=revision-v1');

    expect(screen.getByRole('heading', { name: 'Invoice Parser v1' })).toBeInTheDocument();
    expect(screen.getByText('Historical instructions')).toBeInTheDocument();
    expect(screen.getByText('Historical version')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^edit$/i })).toBeDisabled();
  });

  it('closes the publish dialog when edit permission is revoked', async () => {
    const user = userEvent.setup();
    const original = vi.mocked(useSkill)(SKILL_ID);
    renderAt(SKILL_ID, '?edit=1');
    await user.click(screen.getByRole('button', { name: /new version/i }));
    const versionInput = screen.getByLabelText(/^version$/i);
    try {
      vi.mocked(useSkill).mockReturnValue({ ...original, data: { ...original.data!,
        access: { ...original.data!.access, capabilities: ['view', 'use'] },
      } } as ReturnType<typeof useSkill>);
      fireEvent.change(versionInput, { target: { value: '4' } });
      await waitFor(() => expect(screen.queryByRole('button', { name: /^publish$/i })).not.toBeInTheDocument());
      expect(screen.getByRole('button', { name: /^edit$/i })).toBeDisabled();
      expect(mutations.publish).not.toHaveBeenCalled();
    } finally {
      vi.mocked(useSkill).mockReturnValue(original);
    }
  });
});
