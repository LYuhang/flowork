/**
 * <UserMenuDropdown> tests.
 *
 * Spec `2026-05-25-settings-tab-shell-design.md` §3.1.
 *
 * We use Radix DropdownMenu; in jsdom the menu's portal needs to be
 * attached to the document for openable behavior. Radix handles that
 * automatically. We follow the project's local-i18n convention.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import i18n from 'i18next';
import { MemoryRouter } from 'react-router';
import { useState } from 'react';
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from '@/components/ui/sheet';

// Mock the auth store before importing the component.
const mockLogout = vi.fn(async () => {});
vi.mock('@/stores/auth', () => ({
  useAuthStore: (selector: (s: unknown) => unknown) =>
    selector({
      user: {
        user_id: 'u1',
        tenant_id: 't1',
        email: 'user@example.com',
      },
      logout: mockLogout,
    }),
}));

import { UserMenuDropdown } from '@/components/shared/UserMenuDropdown';

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: {} } },
  interpolation: { escapeValue: false },
});

function renderShell(onNavigate?: () => void) {
  return render(
    <I18nextProvider i18n={testI18n}>
      <MemoryRouter>
        <UserMenuDropdown onNavigate={onNavigate} />
      </MemoryRouter>
    </I18nextProvider>,
  );
}

describe('<UserMenuDropdown>', () => {
  beforeEach(() => {
    mockLogout.mockClear();
  });

  it('renders an avatar trigger with the first letter of the email', () => {
    renderShell();
    const trigger = screen.getByRole('button', { name: /open user menu/i });
    expect(trigger).toHaveTextContent('U'); // 'user@example.com' → 'U'
  });

  it('opens the dropdown with email + Settings + Sign out', async () => {
    renderShell();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /open user menu/i }));
    await waitFor(() => {
      expect(screen.getByText('user@example.com')).toBeInTheDocument();
    });
    expect(
      screen.getByRole('menuitem', { name: /settings/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('menuitem', { name: /sign out/i }),
    ).toBeInTheDocument();
  });

  it('calls logout() when Sign out is clicked', async () => {
    renderShell();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /open user menu/i }));
    const signOut = await screen.findByRole('menuitem', {
      name: /sign out/i,
    });
    await user.click(signOut);
    await waitFor(() => expect(mockLogout).toHaveBeenCalledOnce());
  });

  it('Settings menuitem renders as a link to /settings', async () => {
    renderShell();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /open user menu/i }));
    const settings = await screen.findByRole('menuitem', {
      name: /settings/i,
    });
    const link = settings.closest('a');
    expect(link).toHaveAttribute('href', '/settings');
  });

  it('closes its navigation container when Settings is selected', async () => {
    const onNavigate = vi.fn();
    renderShell(onNavigate);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /open user menu/i }));
    await user.click(await screen.findByRole('menuitem', { name: /settings/i }));
    expect(onNavigate).toHaveBeenCalledOnce();
  });

  it('releases the pointer lock after navigating out of a mobile Sheet', async () => {
    function MobileShell() {
      const [open, setOpen] = useState(false);
      return (
        <Sheet open={open} onOpenChange={setOpen}>
          <SheetTrigger>Navigation</SheetTrigger>
          <SheetContent aria-describedby={undefined}>
            <SheetTitle>Navigation menu</SheetTitle>
            <UserMenuDropdown onNavigate={() => setOpen(false)} />
          </SheetContent>
        </Sheet>
      );
    }
    render(<I18nextProvider i18n={testI18n}><MemoryRouter><MobileShell /></MemoryRouter></I18nextProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Navigation' }));
    await user.click(screen.getByRole('button', { name: /open user menu/i }));
    await user.click(await screen.findByRole('menuitem', { name: /settings/i }));
    await waitFor(() => expect(document.body.style.pointerEvents).not.toBe('none'));
    await user.click(screen.getByRole('button', { name: 'Navigation' }));
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });
});
