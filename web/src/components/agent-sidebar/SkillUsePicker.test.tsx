import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { SkillUsePicker } from './SkillUsePicker';

vi.mock('react-i18next', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-i18next')>()), useTranslation: () => ({ t: (_key: string, fallback: unknown) => typeof fallback === 'string' ? fallback : 'Details' }) }));
vi.mock('@/lib/api/queries/skills', () => ({ useSkills: () => ({
  data: [
    { id: 'id-one', name: 'research', source: 'custom', access: { capabilities: ['use'] } },
    { id: 'id-two', name: 'research', source: 'openai', access: { capabilities: ['use'] } },
    { id: 'no-access', name: 'hidden', source: 'custom', access: { capabilities: ['view'] } },
  ], isPending: false, isError: false,
}) }));

describe('SkillUsePicker', () => {
  it('searches same-name options by ID and returns the chosen identity without auto-selection', async () => {
    const select = vi.fn();
    render(<SkillUsePicker onSelect={select} onClose={vi.fn()} />);
    expect(select).not.toHaveBeenCalled();
    expect(screen.queryByText('hidden')).not.toBeInTheDocument();
    await userEvent.type(screen.getByRole('textbox'), 'id-two');
    expect(screen.getAllByRole('listitem')).toHaveLength(1);
    expect(screen.getByRole('link')).toHaveAttribute('href', '/skills/id-two');
    await userEvent.keyboard('{Enter}');
    expect(select).toHaveBeenCalledWith(expect.objectContaining({ id: 'id-two', name: 'research' }));
  });

  it('supports arrow selection and Escape without sending or changing a selection', async () => {
    const select = vi.fn();
    const close = vi.fn();
    render(<SkillUsePicker onSelect={select} onClose={close} />);
    await userEvent.keyboard('{ArrowDown}{ArrowDown}{Enter}');
    expect(select).toHaveBeenCalledWith(expect.objectContaining({ id: 'id-two' }));
    select.mockClear();
    await userEvent.keyboard('{Escape}');
    expect(close).toHaveBeenCalledOnce();
    expect(select).not.toHaveBeenCalled();
  });
});
