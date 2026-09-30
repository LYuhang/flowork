/**
 * SubAgentNodeEditor — config editor for the SubAgentNode (agent-as-node).
 *
 * Mirrors the harness conventions of the sibling `config-editors.test.tsx`
 * (vitest `isolate:false`, shared module graph): identical-shape mocks for
 * `react-i18next` (fallback-returning `t`) and `@/lib/api/queries/config-options`
 * (`useModelOptions`). We wrap renders in a throwaway QueryClient to match the
 * sibling editor harness.
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// `t('key','fallback')` → fallback (i18n not initialised in tests).
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback ?? _key,
  }),
}));

// Controllable model list for the model dropdown (matches the sibling suite's
// shape so the shared module graph stays consistent under isolate:false).
vi.mock('@/lib/api/queries/config-options', () => ({
  useModelOptions: () => ({ options: ['gpt-4o', 'claude-3-5-sonnet'], isLoading: false }),
}));

// Saved-credential list for the model picker (public projection — no secrets).
vi.mock('@/lib/api/queries/llm-credentials', () => ({
  useLlmCredentials: () => ({ data: [] }),
  useWorkflowModels: () => ({ data: { models: { 'manual-model': { provider: 'openai' } } }, isPending: false, isError: false }),
}));

vi.mock('@/lib/api/queries/skills', () => ({
  useSkills: () => ({ data: [], isSuccess: true, refetch: vi.fn() }),
}));
vi.mock('@/lib/api/queries/mcp-servers', () => ({
  useMcpServers: () => ({ data: [], isSuccess: true, refetch: vi.fn() }),
}));

vi.mock('../CodeMirrorField', () => ({
  CodeMirrorField: ({
    value,
    onCommit,
    'data-testid': testId,
  }: {
    value: string;
    onCommit: (next: string) => void;
    'data-testid'?: string;
  }) => (
    <textarea
      data-testid={testId}
      defaultValue={value}
      onBlur={(event) => onCommit(event.currentTarget.value)}
    />
  ),
}));

import { SubAgentNodeEditor } from '../SubAgentNodeEditor';

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const QcWrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={qc}>{children}</QueryClientProvider>
);

describe('SubAgentNodeEditor', () => {
  it('commits the task_template through onChange on blur', () => {
    const onChange = vi.fn();
    render(
      <SubAgentNodeEditor
        config={{
          task_template: 'Read {{file_path}}.',
          model_name: 'm1',
          max_iterations: 5,
          skills: [{ id: 'skill-1', name: 'Audit' }],
          mcp_servers: [{ id: 'mcp-1', name: 'Orders' }],
        }}
        onChange={onChange}
        outputFieldNames={['answer']}
      />,
      { wrapper: QcWrapper },
    );
    const textarea = screen.getByTestId('cfg-subagent-task-template') as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: 'Summarize {{file_path}}.' } });
    fireEvent.blur(textarea);
    const last = onChange.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    expect(last).toEqual(expect.objectContaining({ task_template: 'Summarize {{file_path}}.' }));
    expect(last.skills).toEqual([{ id: 'skill-1', name: 'Audit' }]);
    expect(last.mcp_servers).toEqual([{ id: 'mcp-1', name: 'Orders' }]);
    expect(last).not.toHaveProperty('system_prompt');
    expect(last).not.toHaveProperty('agent_tools');
  });

  it('uses the professional prompt editor and exposes expand control', () => {
    render(
      <SubAgentNodeEditor
        config={{ task_template: '# Task', model_name: 'm1', max_iterations: 5 }}
        onChange={() => {}}
      />,
      { wrapper: QcWrapper },
    );
    expect(screen.getByTestId('cfg-subagent-task-template')).toBeInTheDocument();
    expect(screen.getByTestId('cfg-subagent-task-expand-btn')).toBeInTheDocument();
  });

  it('commits a new max_iterations number through onChange', () => {
    const onChange = vi.fn();
    render(
      <SubAgentNodeEditor
        config={{
          task_template: 'Read {{file_path}}.',
          model_name: 'm1',
          max_iterations: 5,
        }}
        onChange={onChange}
        outputFieldNames={['answer']}
      />,
      { wrapper: QcWrapper },
    );
    const input = screen.getByTestId('cfg-subagent-max-iterations') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '12' } });
    fireEvent.blur(input);
    const last = onChange.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    expect(last.max_iterations).toBe(12);
  });
});
