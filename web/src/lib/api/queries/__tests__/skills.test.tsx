import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { useSaveCustomSkill, useSkill, useSkills } from '@/lib/api/queries/skills';
import * as client from '@/lib/api/skills';
import type { ReactNode } from 'react';

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe('useSkills', () => {
  beforeEach(() => vi.restoreAllMocks());
  it('returns the listed skills', async () => {
    vi.spyOn(client, 'listSkills').mockResolvedValue([
      { id: 'skill-workflow-builder', name: 'workflow-builder', description: 'build wf', allowed_tools: ['canvas'], version: 1, created_at: null, updated_at: null, access: { capabilities: ['view', 'use'], effective_role: 'viewer', source: 'computed' }, provenance: { ownership_scope: 'platform', origin_type: 'catalog_install', owner: { type: 'platform', display_name: 'Flowork' }, created_by: null } },
    ]);
    const { result } = renderHook(() => useSkills(), { wrapper });
    await waitFor(() => expect(result.current.data?.length).toBe(1));
    expect(result.current.data?.[0].name).toBe('workflow-builder');
  });
});

describe('useSaveCustomSkill', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('fetches the complete detail instead of caching the narrow upload response', async () => {
    const created: client.Skill = {
      id: 'skill-custom',
      name: 'Custom',
      description: 'Uploaded',
      allowed_tools: [],
      version: 1,
      source: 'custom' as const,
      created_at: null,
      updated_at: null,
      access: { capabilities: ['view', 'use', 'update', 'delete', 'manage_access', 'publish'], effective_role: 'manager', source: 'computed' },
      provenance: { ownership_scope: 'personal', origin_type: 'uploaded', owner: { type: 'user', display_name: 'Owner' }, created_by: { type: 'user', display_name: 'Owner' } },
    };
    const detail = {
      ...created,
      body: 'Full instructions',
      skill_md: '# Full instructions',
      files: ['SKILL.md', 'references/contract.txt'],
      has_draft: false,
      draft_updated_at: null,
    };
    vi.spyOn(client, 'saveCustomSkill').mockResolvedValue(created);
    const getSpy = vi.spyOn(client, 'getSkill').mockResolvedValue(detail);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const customWrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    const upload = renderHook(() => useSaveCustomSkill(), {
      wrapper: customWrapper,
    });

    await act(async () => {
      await upload.result.current.mutateAsync({
        bundle: new File(['zip'], 'custom.zip', { type: 'application/zip' }),
      });
    });

    const skill = renderHook(() => useSkill(created.id), {
      wrapper: customWrapper,
    });
    await waitFor(() => expect(skill.result.current.data?.files).toEqual(detail.files));
    expect(getSpy).toHaveBeenCalledWith(created.id);
  });
});
