import { useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { DateTimePicker } from './date-time-picker';
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback, i18n: { resolvedLanguage: 'en' } }) }));

function Form() {
  const [value, setValue] = useState('2026-10-05T09:30:17');
  return <DateTimePicker value={value} onChange={setValue} />;
}
describe('date and time calendar', () => {
  it('keeps the value unchanged until confirmation and preserves seconds', async () => {
    const user = userEvent.setup();
    render(<Form />);
    await user.click(screen.getByRole('button', { name: 'Choose date and time' }));
    await user.click(screen.getByRole('button', { name: '2026-10-06' }));
    fireEvent.change(screen.getByLabelText('second'), { target: { value: '42' } });
    expect(screen.getByLabelText('Run once at')).toHaveValue('2026-10-05 09:30:17');
    await user.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(screen.getByLabelText('Run once at')).toHaveValue('2026-10-06 09:30:42');
    await user.click(screen.getByRole('button', { name: 'Choose date and time' }));
    await user.click(screen.getByRole('button', { name: '2026-10-07' }));
    await user.keyboard('{Escape}');
    expect(screen.getByLabelText('Run once at')).toHaveValue('2026-10-06 09:30:42');
  });
});
