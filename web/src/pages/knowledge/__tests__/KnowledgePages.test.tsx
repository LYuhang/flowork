import { TooltipProvider } from '@/components/ui/tooltip';
import type { ReactElement } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import i18n from 'i18next';
import { createMemoryRouter, RouterProvider } from 'react-router';
import en from '@/lib/i18n/locales/en.json';
import type { ResourceAccess } from '@/lib/api/organizations';
import type { KbListItem } from '@/lib/api/kb';

vi.mock('@/lib/api/kb', () => ({
  getKnowledgeDraft: vi.fn(), getKnowledgeVersions:vi.fn(), getKnowledgeVersion:vi.fn(), getKnowledgeVersionFile:vi.fn(), writeKnowledgeDraftFile:vi.fn(), publishKnowledgeDraft:vi.fn(),
  createKb: vi.fn(), updateKb: vi.fn(),
  importKb: vi.fn(),
  deleteKb: vi.fn(),
  deleteKbFile: vi.fn(),
  getKbFileRaw: vi.fn(),
  getKb: vi.fn(),
  listKbFiles: vi.fn(),
  listKbs: vi.fn(),
  uploadKbFile: vi.fn(),
}));

import {
  getKnowledgeDraft, getKnowledgeVersions, getKnowledgeVersion, getKnowledgeVersionFile, writeKnowledgeDraftFile,
  deleteKb, updateKb,
  getKbFileRaw,
  getKb,
  listKbFiles,
  listKbs,
} from '@/lib/api/kb';
import { KnowledgeDetailPage } from '@/pages/knowledge/KnowledgeDetailPage';
import { KnowledgeListPage } from '@/pages/knowledge/KnowledgeListPage';

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

function renderPage(ui: ReactElement, initialEntry = '/knowledge') {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={testI18n}><TooltipProvider>
        <RouterProvider router={createMemoryRouter([{path:'/knowledge',element:ui},{path:'/knowledge/:kbId',element:ui}],{initialEntries:[initialEntry]})} />
      </TooltipProvider></I18nextProvider>
    </QueryClientProvider>,
  );
}

const managerAccess = {
  capabilities: ['view_metadata', 'view', 'update', 'use', 'manage_access', 'delete'],
  effective_role: 'manager',
  source: 'computed',
} satisfies ResourceAccess;

const detail = {
  id: 'kb-1',
  name: 'Product handbook',
  description: 'Policies and release notes',
  retrieval_strategy: 'agentic_lexical',
  package_version: 3,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
  latest_updated_at: '2026-08-01T00:00:00Z',
  file_count: 2,
  chunk_count: 12,
  stored_count: 0,
  pending_count: 0,
  indexing_count: 0,
  indexed_count: 1,
  failed_count: 1,
  access: managerAccess,
  provenance: {
    ownership_scope: 'personal',
    owner: { type: 'user', display_name: 'Owner' },
    created_by: { type: 'user', display_name: 'Owner' },
    origin_type: 'created',
  },
} satisfies KbListItem;

describe('Knowledge pages', () => {
  beforeEach(() => {
    vi.mocked(listKbs).mockReset();
    vi.mocked(getKb).mockReset();
    vi.mocked(getKbFileRaw).mockReset();
    vi.mocked(getKbFileRaw).mockResolvedValue(new Blob(['# Handbook\n\nRelease trains run every Tuesday.'], { type: 'text/markdown' }));
    vi.mocked(listKbFiles).mockReset();
    vi.mocked(deleteKb).mockReset();
    const snapshot = {name:'Product handbook',description:'Policies and release notes',version:3,base_version:3,latest_version:3,content_hash:'original',has_changes:false,files:['README.md','broken.txt'],readme:'# Handbook',updated_at:''};
    vi.mocked(getKnowledgeDraft).mockResolvedValue(snapshot);
    vi.mocked(getKnowledgeVersion).mockResolvedValue(snapshot);
    vi.mocked(getKnowledgeVersions).mockResolvedValue([{version:3,file_count:2,created_at:''}]);
    vi.mocked(getKnowledgeVersionFile).mockResolvedValue(new Blob(['# Handbook\n\nRelease trains run every Tuesday.'], {type:'text/markdown'}));
    vi.mocked(writeKnowledgeDraftFile).mockReset().mockResolvedValue({...snapshot,files:['README.md'],has_changes:true,content_hash:'changed'});
  });

  it('saves metadata only to the captured draft and keeps the published header after leaving edit mode', async () => {
    vi.mocked(getKb).mockResolvedValue(detail);
    vi.mocked(listKbFiles).mockResolvedValue([]);
    const changed = {name:'Draft name',description:'Draft description',version:0,base_version:3,latest_version:3,content_hash:'new-hash',has_changes:true,files:['README.md'],readme:'# Draft',updated_at:''};
    vi.mocked(updateKb).mockImplementation(async () => {
      vi.mocked(getKnowledgeDraft).mockResolvedValue(changed);
      return changed;
    });
    const user = userEvent.setup();
    renderPage(<KnowledgeDetailPage />, '/knowledge/kb-1');
    await user.click(await screen.findByRole('button', {name:'Edit'}));
    await user.click(screen.getByRole('tab', {name:'Overview'}));
    await user.click(screen.getByRole('button', {name:'Edit Knowledge details'}));
    const name = screen.getByLabelText('Name');
    await user.clear(name);
    await user.type(name, 'Draft name');
    await user.click(screen.getByRole('button', {name:'Save'}));
    await waitFor(() => expect(updateKb).toHaveBeenCalledWith('kb-1', {name:'Draft name',description:'Policies and release notes',expected_hash:'original'}));
    expect(await screen.findByRole('heading', {name:'Draft name'})).toBeInTheDocument();
    await user.click(screen.getByRole('button', {name:'Finish editing'}));
    expect(await screen.findByRole('heading', {name:'Product handbook'})).toBeInTheDocument();
  });

  it('renders searchable knowledge rows and the dedicated empty state', async () => {
    vi.mocked(listKbs).mockResolvedValue([detail]);
    const user = userEvent.setup();
    renderPage(<KnowledgeListPage />);

    expect(await screen.findByText('Product handbook')).toBeInTheDocument();
    expect(screen.getByText('v3')).toBeInTheDocument();
    expect(screen.getByText(/2 files/)).toBeInTheDocument();
    expect(screen.queryByText('Needs attention')).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Filter by status' })).not.toBeInTheDocument();
    await user.type(screen.getByPlaceholderText('Search knowledge bases'), 'missing');
    expect(screen.getByText('No matching knowledge bases')).toBeInTheDocument();
  });

  it('opens the package tree by default and previews the authoritative README', async () => {
    vi.mocked(getKb).mockResolvedValue(detail);
    vi.mocked(listKbFiles).mockResolvedValue([
      {
        id: 'file-1',
        name: 'README.md',
        parser_type: 'markdown',
        mime_type: 'text/markdown',
        file_size: 1024,
        status: 'indexed',
        error_message: null,
        chunk_count: 12,
        created_at: '2026-08-01T00:00:00Z',
        access: detail.access,
        provenance: detail.provenance,
      },
      {
        id: 'file-2',
        name: 'broken.txt',
        parser_type: 'text',
        mime_type: 'text/plain',
        file_size: 20,
        status: 'failed',
        error_message: 'Unsupported encoding',
        chunk_count: 0,
        created_at: '2026-08-01T00:00:00Z',
        access: detail.access,
        provenance: detail.provenance,
      },
    ]);
    const user = userEvent.setup();
    renderPage(<KnowledgeDetailPage />, '/knowledge/kb-1');

    expect(await screen.findByText('Product handbook')).toBeInTheDocument();
    expect(screen.queryByText('How this knowledge base is used')).not.toBeInTheDocument();
    expect(await screen.findByRole('tree', { name: 'Files' })).toBeInTheDocument();
    expect(screen.queryByText('Index status')).not.toBeInTheDocument();
    expect(screen.queryByText('Chunks')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Reindex' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^Edit$/i }));
    await user.click(screen.getAllByRole('button', { name: 'File actions' })[0]!);
    expect(screen.getByRole('menuitem', { name: 'Upload files' })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Upload folder' })).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(await screen.findByText('Release trains run every Tuesday.')).toBeInTheDocument();
    expect(getKnowledgeVersionFile).toHaveBeenCalledWith('kb-1', expect.any(Number), 'README.md');
    await user.click(screen.getByRole('treeitem', { name: /broken.txt/ }));
    expect(screen.getAllByText('broken.txt').length).toBeGreaterThan(0);
    expect(screen.queryByRole('tab', { name: 'Retrieval' })).not.toBeInTheDocument();
  });

  it('offers complete folder and ZIP imports from the knowledge list', async () => {
    vi.mocked(listKbs).mockResolvedValue([]);
    const user = userEvent.setup();
    renderPage(<KnowledgeListPage />);

    await user.click(await screen.findByRole('button', { name: 'Upload folder' }));
    expect(screen.getByRole('dialog', { name: 'Upload a knowledge folder' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Choose folder/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Choose ZIP/ })).toBeInTheDocument();
    expect(screen.getByText(/root README.md describes the package/)).toBeInTheDocument();
  });

  it('requires confirmation before deleting an indexed source', async () => {
    vi.mocked(getKb).mockResolvedValue(detail);
    vi.mocked(listKbFiles).mockResolvedValue([{
      id: 'file-1',
      name: 'handbook.pdf',
      parser_type: 'pdf',
      mime_type: 'application/pdf',
      file_size: 1024,
      status: 'indexed',
      error_message: null,
      chunk_count: 12,
      created_at: '2026-08-01T00:00:00Z',
      access: detail.access,
      provenance: detail.provenance,
    }]);

    const fileSnapshot = {name:'Product handbook',description:'Policies and release notes',version:3,base_version:3,latest_version:3,content_hash:'original',has_changes:false,files:['README.md','handbook.pdf'],readme:'# Handbook',updated_at:''};
    vi.mocked(getKnowledgeDraft).mockResolvedValue(fileSnapshot);
    vi.mocked(getKnowledgeVersion).mockResolvedValue(fileSnapshot);
    const user = userEvent.setup();
    renderPage(<KnowledgeDetailPage />, '/knowledge/kb-1?tab=sources');

    expect((await screen.findAllByText('handbook.pdf')).length).toBeGreaterThan(0);
    await user.click(screen.getByRole('button', { name: /^Edit$/i }));
    fireEvent.contextMenu(screen.getByRole('treeitem', { name: /handbook.pdf/ }));
    await user.click(await screen.findByRole('menuitem', { name: 'Delete' }));
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    expect(screen.getByText('handbook.pdf will be removed from the draft. Published versions are unchanged.')).toBeInTheDocument();
    expect(writeKnowledgeDraftFile).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(writeKnowledgeDraftFile).toHaveBeenCalledWith('kb-1', expect.any(String), 'original'));
  });

  it('requires confirmation before deleting a knowledge base', async () => {
    vi.mocked(getKb).mockResolvedValue(detail);
    vi.mocked(listKbFiles).mockResolvedValue([]);
    vi.mocked(deleteKb).mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage(<KnowledgeDetailPage />, '/knowledge/kb-1');

    await user.click(await screen.findByRole('button', { name: 'Delete' }));
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    expect(screen.getByText('Product handbook and all files in the package will be deleted.')).toBeInTheDocument();
    expect(vi.mocked(deleteKb)).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(deleteKb).toHaveBeenCalledWith('kb-1'));
  });
});
