import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import { MoreHorizontal } from 'lucide-react';
import { SidebarRowMenu } from '../SidebarRowMenu';
import { KeyboardShortcuts } from '../KeyboardShortcuts';
import { MobileAppHeader } from '../MobileAppHeader';
import { CommandPalette } from '@/components/command-palette/CommandPalette';
import { useUIStore } from '@/stores/ui';
import i18n from '@/lib/i18n';

vi.mock('../AppSidebar', () => ({
  AppSidebar: ({ onSearchActions }: { onSearchActions: () => void }) => (
    <>
      <button onClick={onSearchActions}>Search Flowork</button>
      <SidebarRowMenu
        label="Row actions"
        actions={[{ label: 'Rename', icon: MoreHorizontal, onSelect: () => {} }]}
      >
        <span>Test row</span>
      </SidebarRowMenu>
    </>
  ),
}));

describe('mobile command search', () => {
  it('closes only the row menu on the first Escape, then the navigation drawer', async () => {
    await i18n.changeLanguage('en');
    useUIStore.setState({ commandPaletteOpen: false });
    render(<MemoryRouter><KeyboardShortcuts /><MobileAppHeader /></MemoryRouter>);
    const navigation = screen.getByRole('button', { name: 'Open navigation' });
    await userEvent.click(navigation);
    const actions = screen.getByRole('button', { name: 'Row actions' });
    await userEvent.click(actions);
    await screen.findByRole('menuitem', { name: 'Rename' });
    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('menuitem')).toBeNull());
    expect(screen.getByTestId('mobile-nav-drawer')).toBeVisible();
    await waitFor(() => expect(actions).toHaveFocus());
    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByTestId('mobile-nav-drawer')).toBeNull());
    await waitFor(() => expect(navigation).toHaveFocus());
  });

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
