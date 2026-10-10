import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import { MobileAppHeader } from '../MobileAppHeader';
import { CommandPalette } from '@/components/command-palette/CommandPalette';
import { useUIStore } from '@/stores/ui';
import i18n from '@/lib/i18n';

vi.mock('../AppSidebar', () => ({ AppSidebar: ({ onSearchActions }: { onSearchActions: () => void }) => <button onClick={onSearchActions}>Search Flowork</button> }));

describe('mobile command search', () => {
  it('closes the drawer before opening search and restores the navigation trigger', async () => {
    await i18n.changeLanguage('en');
    useUIStore.setState({ commandPaletteOpen: false });
    render(<MemoryRouter><MobileAppHeader /><CommandPalette /></MemoryRouter>);
    const navigation = screen.getByRole('button', { name: 'Open navigation' });
    await userEvent.click(navigation);
    await userEvent.click(screen.getByRole('button', { name: 'Search Flowork' }));
    await screen.findByRole('dialog', { name: 'Search Flowork' });
    expect(screen.queryByTestId('mobile-nav-drawer')).toBeNull();
    expect(screen.getByRole('combobox')).toHaveFocus();
    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(navigation).toHaveFocus());
    expect(useUIStore.getState().commandPaletteOpen).toBe(false);
  });
});
