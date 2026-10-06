import '@/lib/i18n';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { GoalStatus } from '../GoalStatus';
import type { ChatGoal } from '@/lib/api/queries/chats';

const goal: ChatGoal = { objective: 'Finish all remaining rounds', status: 'paused', timeUsedSeconds: 90, tokensUsed: 100, updatedAt: 1000 };
afterEach(() => vi.useRealTimers());
describe('Goal status', () => {
  it('stays hidden without a goal', () => {
    render(<GoalStatus goal={null} streaming={false} disabled={false} onStop={vi.fn()} onCommand={vi.fn()} />);
    expect(screen.queryByRole('button')).toBeNull();
  });
  it('restores the objective and paused elapsed time without advancing it', () => {
    vi.useFakeTimers(); vi.setSystemTime(1000000);
    const command=vi.fn();
    render(<GoalStatus goal={goal} streaming={false} disabled={false} onStop={vi.fn()} onCommand={command} />);
    expect(screen.getByText('0:01:30')).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(60000));
    expect(screen.getByText('0:01:30')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', {name:'Goal details'}));
    expect(screen.getByText(goal.objective)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', {name:'Resume goal'}));
    expect(command).toHaveBeenCalledWith('/goal:resume');
  });
  it('counts active time and connects pause to the existing Stop operation', () => {
    vi.useFakeTimers(); vi.setSystemTime(1000000);
    const stop=vi.fn();
    render(<GoalStatus goal={{...goal,status:'active'}} streaming disabled={false} onStop={stop} onCommand={vi.fn()} />);
    act(() => vi.advanceTimersByTime(3000));
    expect(screen.getByText('0:01:33')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', {name:'Goal details'}));
    fireEvent.click(screen.getByRole('button', {name:'Pause goal'}));
    expect(stop).toHaveBeenCalledOnce();
    expect(screen.getByRole('button', {name:'Clear goal'})).toBeDisabled();
  });
});
