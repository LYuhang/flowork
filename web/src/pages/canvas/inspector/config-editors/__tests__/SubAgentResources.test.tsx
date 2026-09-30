import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { SubAgentResources } from '../SubAgentResources';

const queries = vi.hoisted(() => ({
  skills: { data: [] as Array<Record<string, unknown>>, isSuccess: true, isError: false, refetch: vi.fn() },
  mcp: { data: [] as Array<Record<string, unknown>>, isSuccess: true, isError: false, refetch: vi.fn() },
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: unknown) => typeof fallback === 'string' ? fallback : key,
}) }));
vi.mock('@/lib/api/queries/skills', () => ({ useSkills: () => queries.skills }));
vi.mock('@/lib/api/queries/mcp-servers', () => ({ useMcpServers: () => queries.mcp }));

beforeEach(() => {
  vi.clearAllMocks();
  queries.skills.isSuccess = true;
  queries.skills.isError = false;
  queries.skills.data = [{ id: 'skill-1', name: 'Audit', description: 'Audit orders', version: 1,
    revision_hash: 'a'.repeat(64), access: { capabilities: ['use'] } }];
  queries.mcp.data = [{ id: 'mcp-1', name: 'Orders', enabled: true,
    transport: 'streamable_http', access: { capabilities: ['use'] }, last_tool_names: [] }];
});

describe('SubAgent resource selection', () => {
  it('saves only id/name for both resource types, without a revision', () => {
    const onSkills = vi.fn(), onServers = vi.fn();
    render(<SubAgentResources skills={[]} servers={[]} onSkills={onSkills} onServers={onServers} />);
    fireEvent.click(screen.getByRole('checkbox', { name: /^Audit/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /Orders/ }));
    expect(onSkills).toHaveBeenCalledWith([{ id: 'skill-1', name: 'Audit' }]);
    expect(onServers).toHaveBeenCalledWith([{ id: 'mcp-1', name: 'Orders' }]);
  });

  it('refreshes candidates without requiring an upgrade or modifying the saved selection', () => {
    const onSkills = vi.fn(), onServers = vi.fn();
    const props = { skills: [{ id: 'skill-1', name: 'Audit' }], servers: [], onSkills, onServers };
    const { rerender } = render(<SubAgentResources {...props} />);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh Skills' }));
    expect(queries.skills.refetch).toHaveBeenCalledOnce();
    queries.skills.data = [{ ...queries.skills.data[0], version: 2, revision_hash: 'b'.repeat(64) },
      { ...queries.skills.data[0], id: 'skill-2', name: 'Invoices' }];
    rerender(<SubAgentResources {...props} />);
    expect(screen.getByRole('checkbox', { name: /^Audit/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /Invoices/ })).not.toBeChecked();
    expect(screen.queryByRole('button', { name: /latest published version/i })).not.toBeInTheDocument();
    expect(onSkills).not.toHaveBeenCalled();
  });

  it('preserves missing selections for removal and distinguishes a failed query', () => {
    const onSkills = vi.fn();
    const props = { skills: [{ id: 'gone', name: 'Missing' }], servers: [], onSkills, onServers: vi.fn() };
    queries.skills.data = [];
    queries.skills.isSuccess = false;
    queries.skills.isError = true;
    const { rerender } = render(<SubAgentResources {...props} />);
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load');
    expect(screen.queryByText(/Resource missing/)).not.toBeInTheDocument();
    queries.skills.isSuccess = true;
    queries.skills.isError = false;
    rerender(<SubAgentResources {...props} />);
    expect(screen.getByRole('alert')).toHaveTextContent('Resource missing');
    fireEvent.click(screen.getByRole('button', { name: 'Remove: Missing' }));
    expect(onSkills).toHaveBeenCalledWith([]);
  });
});
