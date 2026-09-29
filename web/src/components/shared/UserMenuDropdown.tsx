/**
 * Topbar user-menu dropdown.
 *
 * Spec `2026-05-25-settings-tab-shell-design.md` §3.1. Avatar circle
 * (first letter of email) → dropdown with email label + Settings link
 * + Sign out item. Self-contained: owns the logout flow, navigates to
 * /login after sign-out. Renders nothing if there is no logged-in user
 * (covers the brief window before `bootstrap()` settles).
 *
 * ThemeToggle stays as a separate topbar icon next to the avatar; it
 * is NOT pulled into this dropdown. Industry split (Linear has theme
 * inside the menu; GitHub / ChatGPT keep it separate); we keep it
 * separate to avoid a 2-click path for a one-click preference and to
 * keep this component focused on identity actions.
 */
import { Link, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { LogOut, Settings as SettingsIcon } from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { useAuthStore } from '@/stores/auth';

export function UserMenuDropdown({ onNavigate }: { onNavigate?: () => void }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);

  if (!user) return null;

  const displayName = user.displayName || user.email;
  const initial = displayName.charAt(0).toUpperCase();

  const handleLogout = async () => {
    await logout();
    navigate('/login', { replace: true });
  };

  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={t('topbar_open_user_menu', 'Open user menu')}
          className="flex h-11 w-11 items-center justify-center rounded-full bg-primary/10 text-sm font-medium text-primary transition-colors hover:bg-primary/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          title={displayName}
          data-action="open-user-menu"
        >
          {initial}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuLabel className="font-normal">
          <div className="flex flex-col">
            <span className="text-xs text-muted-foreground">
              {t('topbar_signed_in_as', 'Signed in as')}
            </span>
            {user.displayName ? (
              <span className="truncate text-sm font-medium">{user.displayName}</span>
            ) : null}
            <span className="truncate text-sm">{user.email}</span>
          </div>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link to="/settings" onClick={onNavigate} data-action="open-settings">
            <SettingsIcon className="mr-2 h-4 w-4" />
            {t('topbar_settings', 'Settings')}
          </Link>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onSelect={handleLogout}
          data-action="logout"
          className="text-destructive focus:text-destructive"
        >
          <LogOut className="mr-2 h-4 w-4" />
          {t('auth_logout', 'Sign out')}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
