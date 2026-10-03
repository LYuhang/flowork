import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { WorkflowTaskList } from '../WorkflowTaskList';
const { query } = vi.hoisted(() => ({ query: vi.fn() }));
vi.mock('@/lib/api/queries/tasks', () => ({ useWorkflowTasks: query }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback || _key }) }));
describe('related workflow tasks', () => {
  it.each(['batch_exec', 'scheduled_run'] as const)('shows pinned versions and copies/links the exact task ID for %s', async taskType => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    query.mockReturnValue({ data: { items: [
      { id: 'task-one', workflow_version: 'v1.sv2', status: 'finished' },
      { id: 'task-two', workflow_version: 'v2.sv3', status: 'paused' },
    ], total: 2 } });
    render(<MemoryRouter><WorkflowTaskList wfId="wf-1" taskType={taskType} /></MemoryRouter>);
    expect(query).toHaveBeenLastCalledWith('wf-1', true, taskType, 0);
    expect(screen.getByText('v1.sv2')).toBeVisible();
    expect(screen.getByText('v2.sv3')).toBeVisible();
    expect(screen.getAllByRole('link', { name: 'Details ↗' }).map(a => a.getAttribute('href'))).toEqual(['/tasks/task-one', '/tasks/task-two']);
    fireEvent.click(screen.getAllByRole('button', { name: 'Copy ID' })[0]);
    expect(writeText).toHaveBeenCalledWith('task-one');
    expect(screen.getByTestId('batch-view-task-center')).toHaveAttribute('href', `/tasks?workflow_id=wf-1&type=${taskType}`);
  });
});
