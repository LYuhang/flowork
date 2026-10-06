import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { TaskNotificationOptions } from '../TaskNotificationOptions';
import { emptyNotifications, notificationsValid } from '../task-notifications';
vi.mock('react-i18next', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-i18next')>()), useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
function Form() {
  const [value, onChange] = useState(emptyNotifications);
  return <><TaskNotificationOptions value={value} onChange={onChange} /><button disabled={!notificationsValid(value)}>Submit</button></>;
}
describe('task notifications', () => {
  it('shows one required recipient for either event and hides it when both are off', () => {
    render(<Form />);
    expect(screen.queryByRole('textbox')).toBeNull();
    fireEvent.click(screen.getByLabelText('Notify on success'));
    expect(screen.getByRole('button')).toBeDisabled();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'test@example.com' } });
    expect(screen.getByRole('button')).not.toBeDisabled();
    fireEvent.click(screen.getByLabelText('Notify on failure'));
    fireEvent.click(screen.getByLabelText('Notify on success'));
    expect(screen.getAllByRole('textbox')).toHaveLength(1);
    expect(screen.getByRole('textbox')).toHaveValue('test@example.com');
    fireEvent.click(screen.getByLabelText('Notify on failure'));
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.getByRole('button')).not.toBeDisabled();
  });
});
