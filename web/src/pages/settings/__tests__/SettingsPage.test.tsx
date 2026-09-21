/**
 * Settings page tab-shell tests.
 *
 * Spec `2026-05-25-settings-tab-shell-design.md` §5.
 *
 * We follow the project's `WorkspacePage.test.tsx` convention: a local
 * minimal i18n instance with empty resources, relying on `t('key',
 * 'default')` fallbacks. This keeps tests fast and isolated from the
 * locale JSON file.
 *
 * MCP servers moved to the unified management page, so the Settings page no
 * no longer has an MCP tab. Preferences and Account remain first-party
 * settings surfaces. (Knowledge Bases was pulled from the UI for v1.)
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import i18n from 'i18next';
import { MemoryRouter, Routes, Route } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { SettingsPage } from '@/pages/settings/SettingsPage';
import {
  disconnectCodexAccount,
  getAgentRuntimeSettings,
  getCodexAccountStatus,
} from '@/lib/api/agent-runtime';

vi.mock('@/lib/api/agent-runtime', () => ({
  getAgentRuntimeSettings: vi.fn(async () => ({
    default_runtime_type: 'codex',
    available_runtime_types: ['codex'],
    runtime_options: [{ id: 'codex', label: 'Codex', description: 'Sandboxed agent runtime.' }],
    codex_managed_profile_id: 'corp-primary',
    preferred_timezone: 'UTC',
    codex_managed_profiles: [{ id: 'corp-primary', name: 'Corporate OpenAI', model_count: 2 }],
    codex_auth_methods: ['chatgpt', 'managed_api', 'personal_api'],
  })),
  setDefaultAgentRuntime: vi.fn(),
  setPreferredTimezone: vi.fn(),
  getCodexAccountStatus: vi.fn(async () => ({
    cli_available: true,
    authenticated: false,
  })),
  getCodexAccountUsage: vi.fn(async () => ({
    email: 'user@example.com',
    plan_type: 'pro',
    rate_limits: [{
      limit_id: 'codex',
      limit_name: null,
      plan_type: 'pro',
      primary: { used_percent: 25, window_duration_mins: 300, resets_at: 1_780_000_000 },
      secondary: null,
      credits: { has_credits: true, unlimited: false, balance: '42' },
      individual_limit: null,
      spend_control_reached: false,
      rate_limit_reached_type: null,
    }],
    rate_limit_reset_credits_available: 0,
    usage_summary: {
      lifetime_tokens: 123_456,
      peak_daily_tokens: 20_000,
      longest_running_turn_sec: 90,
      current_streak_days: 4,
      longest_streak_days: 8,
    },
    daily_usage_buckets: [{ start_date: '2026-08-06', tokens: 1000 }],
    unavailable_sections: [],
    fetched_at: '2026-08-07T12:00:00Z',
  })),
  startCodexDeviceLogin: vi.fn(),
  disconnectCodexAccount: vi.fn(),
}));

vi.mock('@/lib/api/passkeys', () => ({
  getWebAuthnStatus: vi.fn(async () => ({
    enabled: false,
    credentials: [],
    authentication_strength: 'password',
    step_up_expires_at: null,
  })),
  beginWebAuthnRegistration: vi.fn(),
  finishWebAuthnRegistration: vi.fn(),
  deleteWebAuthnCredential: vi.fn(),
}));

vi.mock('@/lib/auth/webauthn-browser', () => ({
  createWebAuthnCredential: vi.fn(),
}));

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: {} } },
  interpolation: { escapeValue: false },
});

function renderAt(url: string) {
  // No ThemeProvider — ThemeToggle uses `useTheme()` which gracefully
  // returns `theme: undefined` without a provider; it renders the
  // system-icon fallback (see ThemeToggle's own comment).
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={testI18n}>
        <MemoryRouter initialEntries={[url]}>
          <Routes>
            <Route path="/settings" element={<SettingsPage />} />
            <Route
              path="/settings/openrouter/callback/:openrouterState"
              element={<SettingsPage />}
            />
          </Routes>
        </MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  );
}

describe('<SettingsPage> tab shell', () => {
  it('renders a vertical tablist with the Preferences tab', () => {
    renderAt('/settings');
    const tablist = screen.getByRole('tablist');
    expect(tablist).toHaveAttribute('aria-orientation', 'vertical');
    const tabs = screen.getAllByRole('tab');
    expect(tabs).toHaveLength(5);
    expect(tabs[0]).toHaveTextContent(/preferences/i);
    expect(tabs[1]).toHaveTextContent(/agent runtime/i);
    expect(tabs[2]).toHaveTextContent(/api keys/i);
    expect(tabs[3]).toHaveTextContent(/extensions/i);
    expect(tabs[4]).toHaveTextContent(/account/i);
    expect(screen.queryByText(/^organization$/i)).toBeNull();
    expect(
      screen.getByTestId('settings-tab-preferences'),
    ).toBeInTheDocument();
    // MCP servers moved to the unified management page — no longer a Settings
    // tab. Knowledge Bases (v1-deferred) + Audit / Usage / Plan also aren't here.
    expect(screen.queryByTestId('settings-tab-mcp')).toBeNull();
    expect(screen.queryByTestId('settings-tab-kb')).toBeNull();
    expect(screen.queryByTestId('settings-tab-audit')).toBeNull();
    expect(screen.queryByTestId('settings-tab-usage')).toBeNull();
    expect(screen.queryByTestId('settings-tab-plan')).toBeNull();
  });

  it('shows Language buttons and the theme toggle inside the Preferences tab', () => {
    renderAt('/settings');
    expect(
      screen.getByRole('button', { name: /中文/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /english/i }),
    ).toBeInTheDocument();
    // ThemeToggle's trigger has aria-label "Toggle theme"
    expect(
      screen.getByRole('button', { name: /toggle theme/i }),
    ).toBeInTheDocument();
  });

  it('reads ?tab=preferences as the active tab (deep-link)', () => {
    renderAt('/settings?tab=preferences');
    const trigger = screen.getByRole('tab', { name: /preferences/i });
    expect(trigger).toHaveAttribute('data-state', 'active');
  });

  it('opens API keys for the provider callback path', () => {
    renderAt('/settings/openrouter/callback/fixture-state?code=fixture-code');
    expect(screen.getByRole('tab', { name: /api keys/i })).toHaveAttribute(
      'data-state',
      'active',
    );
  });

  it('offers the packaged Chrome extension and installation steps', () => {
    renderAt('/settings?tab=extensions');

    expect(screen.getByTestId('settings-extension-panel')).toBeInTheDocument();
    const download = screen.getByRole('link', { name: /download extension/i });
    expect(download).toHaveAttribute('href', '/downloads/flowork-extension.zip');
    expect(download).toHaveAttribute('download', 'flowork-extension.zip');
    expect(screen.getAllByText(/chrome:\/\/extensions/i)).not.toHaveLength(0);
    expect(screen.getByText(/cannot load the zip directly/i)).toBeInTheDocument();
  });

  it('defaults to the preferences tab when no query param is set', () => {
    renderAt('/settings');
    const trigger = screen.getByRole('tab', { name: /preferences/i });
    expect(trigger).toHaveAttribute('data-state', 'active');
  });

  it('keeps Codex account login and provider API keys in separate Settings sections', async () => {
    renderAt('/settings?tab=runtime');

    expect(await screen.findByText('Codex')).toBeInTheDocument();
    expect(await screen.findByText('Codex account')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /sign in with openai/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /api keys/i })).toBeInTheDocument();
    expect(screen.getByText(/api keys are available in the api keys section/i)).toBeInTheDocument();
  });

  it('shows polled account activity and remaining rate-limit capacity after connection', async () => {
    vi.mocked(getCodexAccountStatus).mockResolvedValueOnce({
      cli_available: true,
      authenticated: true,
    });
    renderAt('/settings?tab=runtime');

    expect(await screen.findByTestId('codex-account-usage')).toBeInTheDocument();
    expect(await screen.findByText(/token activity/i)).toBeInTheDocument();
    expect(screen.getByText(/75% remaining/i)).toBeInTheDocument();
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '25');
    expect(screen.getByText(/user@example.com/i)).toBeInTheDocument();
  });

  it('requires confirmation before disconnecting a Codex account', async () => {
    vi.mocked(getCodexAccountStatus).mockResolvedValueOnce({
      cli_available: true,
      authenticated: true,
    });
    vi.mocked(disconnectCodexAccount).mockResolvedValue({
      cli_available: true,
      authenticated: false,
    });
    const user = userEvent.setup();
    renderAt('/settings?tab=runtime');

    await user.click(await screen.findByRole('button', { name: 'Disconnect' }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('Disconnect OpenAI account?')).toBeInTheDocument();
    expect(disconnectCodexAccount).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(disconnectCodexAccount).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Disconnect' }));
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Disconnect' }));
    expect(disconnectCodexAccount).toHaveBeenCalledTimes(1);
  });

  it('hides Codex authentication methods disabled by deployment policy', async () => {
    vi.mocked(getAgentRuntimeSettings).mockResolvedValueOnce({
      default_runtime_type: 'codex',
      available_runtime_types: ['codex'],
      runtime_options: [{ id: 'codex', label: 'Codex', description: 'Sandboxed agent runtime.' }],
      codex_managed_profile_id: 'corp-primary',
      preferred_timezone: 'UTC',
      codex_managed_profiles: [{ id: 'corp-primary', name: 'Corporate OpenAI', model_count: 2 }],
      codex_auth_methods: ['managed_api'],
    });
    renderAt('/settings?tab=runtime');

    expect(await screen.findByText(/openai account sign-in is disabled/i)).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: /openai api/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /sign in with openai/i })).toBeNull();
    expect(screen.getByText(/configured under api keys/i)).toBeInTheDocument();
  });

  it('shows passkey security without initial-release authenticator MFA', async () => {
    renderAt('/settings?tab=account');

    expect(await screen.findByTestId('passkey-security-section')).toBeInTheDocument();
    expect(await screen.findByText(/passkeys and security keys/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /add passkey/i })).toBeInTheDocument();
    expect(screen.queryByText(/multi-factor authentication/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/authenticator app/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /delete account/i })).toBeInTheDocument();
  });

});
