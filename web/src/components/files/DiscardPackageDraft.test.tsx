import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { DiscardPackageDraft } from './DiscardPackageDraft';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

describe('discard package draft', () => {
  it('captures the draft hash when confirmation opens instead of deleting newer edits', async () => {
    const discard = vi.fn().mockResolvedValue(undefined);
    const view = render(<DiscardPackageDraft expectedHash="old" onDiscard={discard} />);
    fireEvent.click(screen.getByRole('button', { name: 'files.manage.discardDraft' }));
    view.rerender(<DiscardPackageDraft expectedHash="new" onDiscard={discard} />);
    fireEvent.click(screen.getAllByRole('button', { name: 'files.manage.discardDraft' }).at(-1)!);
    await waitFor(() => expect(discard).toHaveBeenCalledWith('old'));
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
  });

  it('keeps confirmation open on error and allows cancel without another request', async () => {
    const discard = vi.fn().mockRejectedValue(new Error('package_file_changed'));
    render(<DiscardPackageDraft expectedHash="old" onDiscard={discard} />);
    fireEvent.click(screen.getByRole('button', { name: 'files.manage.discardDraft' }));
    fireEvent.click(screen.getAllByRole('button', { name: 'files.manage.discardDraft' }).at(-1)!);
    await waitFor(() => expect(screen.getByRole('button', {name: 'cancel'})).toBeEnabled());
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', {name: 'cancel'}));
    expect(discard).toHaveBeenCalledTimes(1);
  });
});
