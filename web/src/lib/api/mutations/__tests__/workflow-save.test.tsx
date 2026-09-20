import type { ReactNode } from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { toast } from 'sonner';
import { server, fixtureWorkflow } from '@/__tests__/msw-handlers';
import { useCommitWorkflow } from '@/lib/api/mutations/workflow-ops';
import { useWorkflowEditStore } from '@/stores/workflow-edit';
import i18n from '@/lib/i18n';

beforeEach(async () => {
  await i18n.changeLanguage('en');
  useWorkflowEditStore.getState().setDraft({ __meta__: { name: 'Before' } });
  useWorkflowEditStore.getState().applyEdit(() => ({ __meta__: { name: 'Unsaved' } }));
});

function setup() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(() => useCommitWorkflow('wf_test_1'), { wrapper });
}

describe('workflow save responses', () => {
  it('shows the structured failure and preserves draft, baseline and undo history', async () => {
    server.use(http.post('*/api/v1/workflows/wf_test_1/commits', () =>
      HttpResponse.json({ detail: { code: 'invalid_request_origin' } }, { status: 403 })));
    const error = vi.spyOn(toast, 'error');
    const before = useWorkflowEditStore.getState();
    const { result } = setup();
    act(() => result.current.mutate(before.draft!));
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(error).toHaveBeenCalledWith(`Save failed: ${i18n.t('api.error.invalidRequestOrigin')}`);
    const after = useWorkflowEditStore.getState();
    expect(after.draft).toEqual(before.draft);
    expect(after.baseline).toBe(before.baseline);
    expect(after.undoStack).toEqual(before.undoStack);
    expect(after.isDirty()).toBe(true);
    error.mockRestore();
  });

  it('marks the draft saved only after a successful response', async () => {
    server.use(http.post('*/api/v1/workflows/wf_test_1/commits', () =>
      HttpResponse.json(fixtureWorkflow({ active_sv: 3 }))));
    const success = vi.spyOn(toast, 'success');
    const { result } = setup();
    act(() => result.current.mutate(useWorkflowEditStore.getState().draft!));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(useWorkflowEditStore.getState().isDirty()).toBe(false);
    expect(success).toHaveBeenCalledWith('Saved');
    success.mockRestore();
  });
});
