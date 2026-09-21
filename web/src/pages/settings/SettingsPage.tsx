/**
 * `/settings` — user preferences page.
 *
 * Spec `2026-05-25-settings-tab-shell-design.md`: reshapes the page from a
 * flat list of cards into a vertical-left Tabs shell. The MVP single tab
 * `preferences` holds the existing Language + Theme cards. Future tabs
 * (Profile / API tokens / MCP Servers / Sessions / etc.) each add one
 * `<TabsTrigger>` + one `<TabsContent>` — zero structural change.
 *
 * Active tab is reflected in the URL as `?tab=<id>` for deep-linking.
 */
import { useTranslation } from 'react-i18next';
import { useNavigate, useParams, useSearchParams } from 'react-router';
import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@/components/ui/tabs';
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { ThemeToggle } from '@/components/shared/ThemeToggle';
import { setLocale, type Locale } from '@/lib/i18n';
import { browserTimezone, setTimezone, useTimezone } from '@/lib/timezone';
import { CURATED_ZONE_IDS, TIMEZONE_GROUPS } from '@/lib/timezone-list';
import { AuthApiError, useAuthStore } from '@/stores/auth';
import {
  getAgentRuntimeSettings,
  disconnectCodexAccount,
  getCodexAccountStatus,
  setDefaultAgentRuntime,
  setPreferredTimezone,
  startCodexDeviceLogin,
  type AgentRuntimeSettings,
  type AgentRuntimeType,
  type CodexAccountStatus,
  type CodexDeviceLogin,
} from '@/lib/api/agent-runtime';
import {
  codexAccountUsageQueryKey,
  runtimeCapabilitiesPrefix,
} from '@/lib/api/queries/agent-runtime';
import { OrganizationSettingsPanel } from '@/pages/settings/OrganizationSettingsPanel';
import { PasskeySecuritySection } from '@/pages/settings/PasskeySecuritySection';
import { CodexAccountUsagePanel } from '@/pages/settings/CodexAccountUsagePanel';
import { ExtensionSettingsPanel } from '@/pages/settings/ExtensionSettingsPanel';
import { CredentialsSettingsPanel } from '@/pages/credentials/CredentialsPage';
import { ActionableError } from '@/components/presentation/ActionableError';
import { SectionBlock } from '@/components/layout/section-block';
import { organizationsQueryKey } from '@/lib/api/organization-query-keys';
import { listOrganizations } from '@/lib/api/organizations';
import { getApiBase } from '@/lib/base-path';

interface LanguageOption {
  value: Locale;
  label: string;
}

const LANGUAGES: LanguageOption[] = [
  { value: 'zh', label: '中文' },
  { value: 'en', label: 'English' },
];

const DEFAULT_TAB = 'preferences';

interface AccountDeletionPolicy {
  account_deletion_mode: 'immediate' | 'delayed';
  account_deletion_retention_days: number;
}

async function getAccountDeletionPolicy(): Promise<AccountDeletionPolicy> {
  const response = await fetch(`${getApiBase()}/api/v1/public-config`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const payload = await response.json() as Partial<AccountDeletionPolicy>;
  return {
    account_deletion_mode:
      payload.account_deletion_mode === 'delayed' ? 'delayed' : 'immediate',
    account_deletion_retention_days:
      typeof payload.account_deletion_retention_days === 'number'
        ? payload.account_deletion_retention_days
        : 14,
  };
}

/**
 * Timezone preference card. A grouped <Select> of curated, readable IANA
 * zones with the browser-detected zone surfaced at the very top so the user's
 * obvious choice is one click away. On change → `setTimezone` (persists +
 * re-renders every timestamp via the store). All human-facing times across the
 * app (workflow updated time, task/deployment times) honour this zone.
 */
function TimezoneCard() {
  const { t } = useTranslation();
  const current = useTimezone();
  const detected = browserTimezone();
  // Avoid duplicating the detected zone if it's already in the curated list.
  const detectedIsCurated = CURATED_ZONE_IDS.includes(detected);
  const updateTimezone = (next: string) => {
    if (next === current) return;
    const previous = current;
    setTimezone(next);
    void setPreferredTimezone(next).catch((reason: unknown) => {
      setTimezone(previous);
      const message = reason instanceof Error ? reason.message : String(reason);
      toast.error(message);
    });
  };

  return (
    <section className="px-4 py-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <h3 className="text-sm font-medium">
            {t('settings_timezone', 'Timezone')}
          </h3>
          <p className="mt-0.5 text-sm text-muted-foreground">
            {t(
              'settings_timezone_desc',
              'Choose your account timezone for displayed dates and new Agent conversations.',
            )}
          </p>
        </div>
        <div className="w-full flex-shrink-0 sm:w-64">
          <Select value={current} onValueChange={updateTimezone}>
            <SelectTrigger data-testid="settings-timezone-select" aria-label={t('settings_timezone', 'Timezone')}>
              <SelectValue
                placeholder={t('settings_timezone', 'Timezone')}
              />
            </SelectTrigger>
            <SelectContent>
              {!detectedIsCurated ? (
                <SelectGroup>
                  <SelectLabel>
                    {t('settings_timezone_detected', 'Detected')}
                  </SelectLabel>
                  <SelectItem value={detected}>{detected}</SelectItem>
                </SelectGroup>
              ) : null}
              {TIMEZONE_GROUPS.map((group) => (
                <SelectGroup key={group.region}>
                  <SelectLabel>{group.region}</SelectLabel>
                  {group.zones.map((zone) => (
                    <SelectItem key={zone.value} value={zone.value}>
                      {zone.label}
                    </SelectItem>
                  ))}
                </SelectGroup>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
    </section>
  );
}

function AgentRuntimePanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [settings, setSettings] = useState<AgentRuntimeSettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    void getAgentRuntimeSettings()
      .then((value) => {
        if (active) setSettings(value);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      active = false;
    };
  }, []);

  const updateRuntime = async (runtimeType: AgentRuntimeType) => {
    if (!settings || runtimeType === settings.default_runtime_type) return;
    setSaving(true);
    setError('');
    try {
      const next = await setDefaultAgentRuntime(runtimeType);
      setSettings(next);
      // New Chat selectors are Runtime capability-driven. Invalidate every
      // capability projection so any unstarted composer changes immediately.
      // Persisted chats are safe: the backend answers with their fixed binding.
      await queryClient.invalidateQueries({
        queryKey: runtimeCapabilitiesPrefix,
      });
      await queryClient.refetchQueries({
        queryKey: runtimeCapabilitiesPrefix,
        type: 'active',
      });
      toast.success(t('settings_runtime_saved', 'Default runtime updated'));
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  };

  const available = new Set(settings?.available_runtime_types ?? []);
  const runtimeOptions = (settings?.runtime_options ?? []).filter((option) =>
    available.has(option.id)
  );

  return (
    <>
      <SectionBlock
        title={t('settings_runtime_default', 'Default Agent runtime')}
        description={t('settings_runtime_desc', 'Used when a new chat starts. Existing chats keep the runtime they were created with.')}
      >
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0 max-w-[65ch]">
            {error ? (
              <ActionableError
                className="mt-3"
                title={t('settings_runtime_error', 'Could not update Agent runtime settings')}
                description={t('settings_runtime_error_hint', 'Check the connection and runtime configuration, then try again.')}
                technicalDetails={error}
                technicalDetailsLabel={t('common.technicalDetails', 'Technical details')}
              />
            ) : null}
          </div>
          <div className="w-full shrink-0 sm:w-64">
            <Select
              value={settings?.default_runtime_type ?? 'codex'}
              onValueChange={(value) =>
                void updateRuntime(value as AgentRuntimeType)
              }
              disabled={!settings || saving || runtimeOptions.length <= 1}
            >
              <SelectTrigger data-testid="settings-agent-runtime-select" aria-label={t('settings_runtime_default', 'Default Agent runtime')}>
                <SelectValue
                  placeholder={t(
                    'settings_runtime_loading',
                    'Loading runtime…',
                  )}
                />
              </SelectTrigger>
              <SelectContent>
                {runtimeOptions.map((option) => (
                  <SelectItem key={option.id} value={option.id}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {saving ? (
              <p
                className="mt-1.5 text-xs text-muted-foreground"
                aria-live="polite"
              >
                {t('settings_runtime_saving', 'Saving…')}
              </p>
            ) : null}
          </div>
        </div>
      </SectionBlock>
      {settings
      && settings.default_runtime_type === 'codex'
      && available.has('codex') ? (
        <CodexConnectionsPanel
          settings={settings}
        />
      ) : null}
    </>
  );
}

function CodexConnectionsPanel({
  settings,
}: {
  settings: AgentRuntimeSettings;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [account, setAccount] = useState<CodexAccountStatus | null>(null);
  const [deviceLogin, setDeviceLogin] = useState<CodexDeviceLogin | null>(null);
  const [disconnectOpen, setDisconnectOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const authMethods = new Set(settings.codex_auth_methods ?? []);
  const chatgptAllowed = authMethods.has('chatgpt');

  useEffect(() => {
    if (!chatgptAllowed) return undefined;
    let active = true;
    void getCodexAccountStatus()
      .then((value) => active && setAccount(value))
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      active = false;
    };
  }, [chatgptAllowed]);

  useEffect(() => {
    if (!deviceLogin || account?.authenticated) return undefined;
    const timer = window.setInterval(() => {
      if (Date.now() >= Date.parse(deviceLogin.expires_at)) {
        window.clearInterval(timer);
        setDeviceLogin(null);
        setError(t('settings_codex_device_expired', 'The sign-in code expired. Start again.'));
        return;
      }
      void getCodexAccountStatus().then((value) => {
        setAccount(value);
        if (value.authenticated) {
          setDeviceLogin(null);
          void queryClient.invalidateQueries({ queryKey: runtimeCapabilitiesPrefix });
          void queryClient.invalidateQueries({ queryKey: codexAccountUsageQueryKey });
          toast.success(t('settings_codex_connected', 'Codex account connected'));
        }
      }).catch(() => undefined);
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [account?.authenticated, deviceLogin, queryClient, t]);

  const startLogin = async () => {
    setBusy(true);
    setError('');
    try {
      const login = await startCodexDeviceLogin();
      setDeviceLogin(login);
      window.open(login.verification_url, '_blank', 'noopener,noreferrer');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    setBusy(true);
    setError('');
    try {
      setAccount(await disconnectCodexAccount());
      setDisconnectOpen(false);
      queryClient.removeQueries({ queryKey: codexAccountUsageQueryKey });
      await queryClient.invalidateQueries({ queryKey: runtimeCapabilitiesPrefix });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="border-b border-edge-subtle py-5" data-testid="codex-connections-panel">
      <div className="max-w-[65ch]">
        <h3 className="text-sm font-medium">
          {t('settings_codex_connection', 'Codex account')}
        </h3>
        <p className="mt-1 text-sm text-muted-foreground">
          {t('settings_codex_connection_desc', 'Connect the OpenAI account used by Codex. Provider API keys are available in the API keys section of Settings, and models are selected when you start a chat.')}
        </p>
      </div>

      {!chatgptAllowed ? (
        <p className="mt-3 text-sm text-muted-foreground">
          {t('settings_codex_account_disabled', 'OpenAI account sign-in is disabled for this deployment. You can still use Codex API models configured under API Keys.')}
        </p>
      ) : <div className="mt-4 max-w-xl">
          <p className="text-sm text-muted-foreground">
            {t('settings_codex_account_help', 'Sign in with a one-time device code. No public callback URL is required.')}
          </p>
          {!account ? (
            <p className="mt-3 text-sm text-muted-foreground" aria-live="polite">
              {t('settings_runtime_loading', 'Loading runtime…')}
            </p>
          ) : !account.cli_available ? (
            <p className="mt-3 text-sm text-destructive" role="alert">
              {t('settings_codex_cli_unavailable', 'Codex CLI is not installed on this deployment.')}
            </p>
          ) : account.authenticated ? (
            <div className="mt-3">
              <div className="flex items-center gap-3">
                <span className="text-sm font-medium text-state-success">
                  {t('settings_codex_connected', 'Connected')}
                </span>
                <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => setDisconnectOpen(true)}>
                  {t('settings_codex_disconnect', 'Disconnect')}
                </Button>
              </div>
              <CodexAccountUsagePanel />
            </div>
          ) : deviceLogin ? (
            <div className="mt-3 rounded-lg border border-edge-subtle bg-surface-subtle p-4">
              <p className="text-sm font-medium">
                {t('settings_codex_device_title', 'Confirm this code with OpenAI')}
              </p>
              <div className="mt-3 flex flex-wrap items-center gap-3">
                <code className="select-all rounded-md border border-edge-subtle bg-background px-3 py-2 text-base font-semibold tracking-[0.18em]">
                  {deviceLogin.user_code}
                </code>
                <Button type="button" size="sm" asChild>
                  <a href={deviceLogin.verification_url} target="_blank" rel="noreferrer">
                    {t('settings_codex_open_login', 'Open verification page')}
                  </a>
                </Button>
              </div>
              <p className="mt-2 text-xs text-muted-foreground" aria-live="polite">
                {t('settings_codex_waiting', 'Waiting for confirmation…')}
              </p>
            </div>
          ) : (
            <Button type="button" className="mt-3" size="sm" disabled={busy} onClick={() => void startLogin()}>
              {busy ? t('settings_codex_connecting', 'Connecting…') : t('settings_codex_sign_in', 'Sign in with OpenAI')}
            </Button>
          )}
        </div>}

      <Dialog
        open={disconnectOpen}
        onOpenChange={(open) => {
          if (!busy) setDisconnectOpen(open);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('settings_codex_disconnect_title', 'Disconnect OpenAI account?')}</DialogTitle>
            <DialogDescription>
              {t(
                'settings_codex_disconnect_description',
                'New Codex chats will not be able to use this account until you sign in again. Existing chat data is not deleted.',
              )}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="outline" disabled={busy} onClick={() => setDisconnectOpen(false)}>
              {t('cancel', 'Cancel')}
            </Button>
            <Button type="button" variant="destructive" disabled={busy} onClick={() => void disconnect()}>
              {busy ? t('settings_codex_disconnecting', 'Disconnecting…') : t('settings_codex_disconnect', 'Disconnect')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {error ? (
        <ActionableError
          className="mt-3"
          title={t('settings_codex_connection_error', 'Could not update the Codex connection')}
          description={t('settings_codex_connection_error_hint', 'Review the OpenAI account connection, then try again.')}
          technicalDetails={error}
          technicalDetailsLabel={t('common.technicalDetails', 'Technical details')}
        />
      ) : null}
    </section>
  );
}

function DeleteAccountCard() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const deleteAccount = useAuthStore((s) => s.deleteAccount);
  const [email, setEmail] = useState('');
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const policy = useQuery({
    queryKey: ['public-config', 'account-deletion'],
    queryFn: getAccountDeletionPolicy,
    staleTime: 5 * 60 * 1000,
  });

  const delayed = policy.data?.account_deletion_mode === 'delayed';
  const retentionDays = policy.data?.account_deletion_retention_days ?? 14;

  const expectedEmail = user?.email ?? '';
  const emailMatches =
    email.trim().toLowerCase() === expectedEmail.toLowerCase();

  const handleDelete = async () => {
    setSubmitting(true);
    try {
      await deleteAccount(email.trim());
      toast.success(
        delayed
          ? t(
              'account_delete_success_delayed',
              `Account locked. Deletion is scheduled in ${retentionDays} days.`,
              { days: retentionDays },
            )
          : t(
              'account_delete_success_immediate',
              'Account locked and permanent deletion started.',
            ),
      );
      navigate('/login', { replace: true });
    } catch (err) {
      const message =
        err instanceof AuthApiError
          ? err.detail
          : err instanceof Error
            ? err.message
            : String(err);
      toast.error(message);
      setSubmitting(false);
      setConfirmOpen(false);
    }
  };

  return (
    <section className="mt-4 border-t border-state-danger/35 bg-state-danger/5 px-5 py-6">
      <div className="flex flex-col gap-5">
        <div className="min-w-0">
          <h3 className="text-sm font-medium text-destructive">
            {t('account_delete_title', 'Delete account')}
          </h3>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            {delayed
              ? t(
                  'account_delete_desc_delayed',
                  `Your account and personal services are locked immediately. Permanent deletion starts after ${retentionDays} days unless you cancel from the sign-in page.`,
                  { days: retentionDays },
                )
              : t(
                  'account_delete_desc_immediate',
                  'Your account and personal services are locked immediately, then permanent deletion begins. This cannot be cancelled.',
                )}
          </p>
          <p className="mt-2 max-w-2xl text-xs text-muted-foreground">
            {t(
              'account_delete_organization_policy',
              'Personal chats, workflows, tasks, API keys, files, credentials, and Runtime state are removed. Content owned by an organization remains and is attributed to Deleted user. You must transfer ownership before deleting the sole Owner of an organization.',
            )}
          </p>
        </div>

        <div className="grid gap-2">
          <Label htmlFor="delete-account-email">
            {t('account_delete_email_label', 'Type your email to confirm')}
          </Label>
          <Input
            id="delete-account-email"
            type="email"
            value={email}
            autoComplete="off"
            placeholder={expectedEmail}
            onChange={(event) => setEmail(event.target.value)}
            className="max-w-md"
          />
        </div>

        <div>
          <Button
            type="button"
            variant="destructive"
            disabled={!emailMatches || submitting}
            onClick={() => setConfirmOpen(true)}
          >
            {t('account_delete_button', 'Delete account')}
          </Button>
        </div>
      </div>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {t('account_delete_confirm_title', 'Delete this account?')}
            </DialogTitle>
            <DialogDescription>
              {delayed
                ? t(
                    'account_delete_confirm_desc_delayed',
                    `Access stops now. You can cancel from the sign-in page during the ${retentionDays}-day retention period.`,
                    { days: retentionDays },
                  )
                : t(
                    'account_delete_confirm_desc_immediate',
                    'Access stops now and permanent deletion starts immediately. This cannot be undone.',
                  )}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={submitting}
              onClick={() => setConfirmOpen(false)}
            >
              {t('common_cancel', 'Cancel')}
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={submitting}
              onClick={handleDelete}
            >
              {submitting
                ? t('account_delete_deleting', 'Deleting...')
                : delayed
                  ? t('account_delete_confirm_button_delayed', 'Lock and schedule deletion')
                  : t('account_delete_confirm_button_immediate', 'Delete permanently')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

export function SettingsPage() {
  const { t, i18n } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const { openrouterState } = useParams<{ openrouterState?: string }>();
  const active = (i18n.resolvedLanguage ?? i18n.language) as Locale;
  const activeOrganizationId = useAuthStore((state) => state.user?.tenant_id ?? '');
  const organizations = useQuery({
    queryKey: organizationsQueryKey,
    queryFn: listOrganizations,
    enabled: Boolean(activeOrganizationId),
  });
  const activeOrganization = organizations.data?.items.find(
    (item) => item.organization_id === activeOrganizationId,
  );
  const showOrganization = activeOrganization?.kind === 'business';
  const organizationLabel = activeOrganization?.role === 'member'
    || activeOrganization?.role === 'guest'
    ? t('settings_tab_my_organization', 'My organization')
    : t('settings_tab_organization', 'Organization');

  const requestedTab = openrouterState
    ? 'api-keys'
    : searchParams.get('tab') ?? DEFAULT_TAB;
  const visibleTabs = showOrganization
    ? ['preferences', 'runtime', 'api-keys', 'extensions', 'organization', 'account']
    : ['preferences', 'runtime', 'api-keys', 'extensions', 'account'];
  const currentTab = visibleTabs.includes(requestedTab)
    ? requestedTab
    : DEFAULT_TAB;

  const handleTabChange = (value: string) => {
    setSearchParams({ tab: value });
  };

  return (
    <div className="page-shell">
      <div className="page-content w-full max-w-5xl gap-6">
        <header className="hidden border-b border-edge-subtle pb-5 md:block">
          <h1 className="text-title">
            {t('settings_title', 'Settings')}
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {t(
              'settings_subtitle',
              'Manage your workspace preferences, integrations, and account.',
            )}
          </p>
        </header>

        <div className="md:hidden">
          <Select value={currentTab} onValueChange={handleTabChange}>
            <SelectTrigger aria-label={t('settings_section', 'Settings section')}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="preferences">{t('settings_tab_preferences', 'Preferences')}</SelectItem>
              <SelectItem value="runtime">{t('settings_tab_runtime', 'Agent runtime')}</SelectItem>
              <SelectItem value="api-keys">{t('settings_tab_api_keys', 'API keys')}</SelectItem>
              <SelectItem value="extensions">{t('settings_tab_extensions', 'Extensions')}</SelectItem>
              {showOrganization ? <SelectItem value="organization">{organizationLabel}</SelectItem> : null}
              <SelectItem value="account">{t('settings_tab_account', 'Account')}</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <Tabs
          value={currentTab}
          onValueChange={handleTabChange}
          orientation="vertical"
          className="flex items-start gap-8"
        >
          {/* Left vertical nav + right content. Robustness rules that prevent
              the earlier "cards squish to one-word-per-line" bug:
                - the nav is a FIXED width and `shrink-0` (one width class, no
                  `w-full`/breakpoint that could leak and eat the row);
                - the content column is `flex-1 min-w-0` so it always fills the
                  remaining width and can shrink instead of overflowing.
              No `lg:` breakpoint dependency, so it behaves identically on a
              narrow/proxy viewport. */}
          <TabsList
            aria-orientation="vertical"
            variant="vertical"
            className="hidden h-auto w-52 shrink-0 bg-transparent p-0 md:flex"
          >
            <TabsTrigger
              value="preferences"
              className="w-full justify-start"
              data-testid="settings-tab-preferences"
            >
              {t('settings_tab_preferences', 'Preferences')}
            </TabsTrigger>
            <TabsTrigger
              value="runtime"
              className="w-full justify-start"
              data-testid="settings-tab-runtime"
            >
              {t('settings_tab_runtime', 'Agent runtime')}
            </TabsTrigger>
            <TabsTrigger
              value="api-keys"
              className="w-full justify-start"
              data-testid="settings-tab-api-keys"
            >
              {t('settings_tab_api_keys', 'API keys')}
            </TabsTrigger>
            <TabsTrigger
              value="extensions"
              className="w-full justify-start"
              data-testid="settings-tab-extensions"
            >
              {t('settings_tab_extensions', 'Extensions')}
            </TabsTrigger>
            {showOrganization ? (
              <TabsTrigger
                value="organization"
                className="w-full justify-start"
                data-testid="settings-tab-organization"
              >
                {organizationLabel}
              </TabsTrigger>
            ) : null}
            <TabsTrigger
              value="account"
              className="w-full justify-start"
              data-testid="settings-tab-account"
            >
              {t('settings_tab_account', 'Account')}
            </TabsTrigger>
          </TabsList>

          {/* Single flex-1 content column — wraps every TabsContent so the
              right side is one stable, full-height region (no empty band). */}
          <div className="min-w-0 flex-1">
              <TabsContent
                value="preferences"
                className="mt-0 flex w-full min-w-0 max-w-3xl flex-col gap-6"
              >
                <header className="min-w-0">
                  <h2 className="text-base font-medium">
                    {t('settings_tab_preferences', 'Preferences')}
                  </h2>
                  <p className="mt-0.5 text-sm text-muted-foreground">
                    {t(
                      'preferences_subtitle',
                      'Language and appearance for this device. Saved to this browser.',
                    )}
                  </p>
                </header>

                <SectionBlock
                  title={t('settings_interface', 'Interface')}
                  description={t('settings_interface_desc', 'Choose how Flowork looks and formats information for you.')}
                  contentClassName="divide-y divide-edge-subtle p-0"
                >
                {/* Language */}
                <section className="px-4 py-4">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0">
                      <h3 className="text-sm font-medium">
                        {t('settings_language', 'Language')}
                      </h3>
                      <p className="mt-0.5 text-sm text-muted-foreground">
                        {t(
                          'settings_language_desc',
                          'Switch the interface language. Saved to this browser.',
                        )}
                      </p>
                    </div>
                    <div className="flex flex-shrink-0 gap-2">
                      {LANGUAGES.map((opt) => (
                        <Button
                          key={opt.value}
                          variant={active === opt.value ? 'default' : 'outline'}
                          size="sm"
                          data-action={`set-locale-${opt.value}`}
                          aria-pressed={active === opt.value}
                          onClick={() => setLocale(opt.value)}
                        >
                          {opt.label}
                        </Button>
                      ))}
                    </div>
                  </div>
                </section>

                {/* Theme */}
                <section className="px-4 py-4">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0">
                      <h3 className="text-sm font-medium">
                        {t('settings_theme', 'Theme')}
                      </h3>
                      <p className="mt-0.5 text-sm text-muted-foreground">
                        {t(
                          'settings_theme_desc',
                          'Switch between light, dark, or follow your system preference.',
                        )}
                      </p>
                    </div>
                    <div className="flex flex-shrink-0 items-center">
                      <ThemeToggle />
                    </div>
                  </div>
                </section>

                {/* Timezone */}
                <TimezoneCard />
                </SectionBlock>
              </TabsContent>

              <TabsContent
                value="runtime"
                className="mt-0 flex w-full min-w-0 max-w-3xl flex-col gap-6"
              >
                <header className="min-w-0">
                  <h2 className="text-base font-medium">
                    {t('settings_tab_runtime', 'Agent runtime')}
                  </h2>
                  <p className="mt-0.5 max-w-[65ch] text-sm text-muted-foreground">
                    {t(
                      'settings_runtime_subtitle',
                      'Choose the sandboxed Agent SDK used by new conversations.',
                    )}
                  </p>
                </header>
                <AgentRuntimePanel />
              </TabsContent>

              <TabsContent
                value="api-keys"
                className="mt-0 flex w-full min-w-0 max-w-3xl flex-col gap-6"
              >
                <CredentialsSettingsPanel />
              </TabsContent>

              <TabsContent
                value="extensions"
                className="mt-0 flex w-full min-w-0 max-w-3xl flex-col gap-6"
              >
                <header className="min-w-0">
                  <h2 className="text-base font-medium">
                    {t('settings_tab_extensions', 'Extensions')}
                  </h2>
                  <p className="mt-0.5 max-w-[65ch] text-sm text-muted-foreground">
                    {t(
                      'settings_extensions_subtitle',
                      'Download and configure companion apps built for this Flowork deployment.',
                    )}
                  </p>
                </header>
                <ExtensionSettingsPanel />
              </TabsContent>

              {showOrganization ? (
                <TabsContent
                  value="organization"
                  className="mt-0 flex w-full min-w-0 flex-col gap-6"
                >
                  <header className="min-w-0">
                    <h2 className="text-base font-medium">{organizationLabel}</h2>
                    <p className="mt-0.5 max-w-[65ch] text-sm text-muted-foreground">
                      {activeOrganization?.role === 'member' || activeOrganization?.role === 'guest'
                        ? t('organization.mySettingsDescription', 'Review your company membership, role, and assigned teams.')
                        : t('organization.settingsDescription', 'Manage company people, departments, identity, and operational accounts according to your role.')}
                    </p>
                  </header>
                  <OrganizationSettingsPanel />
                </TabsContent>
              ) : null}

              <TabsContent
                value="account"
                className="mt-0 flex w-full min-w-0 max-w-3xl flex-col gap-6"
              >
                <header className="min-w-0">
                  <h2 className="text-base font-medium">
                    {t('settings_tab_account', 'Account')}
                  </h2>
                  <p className="mt-0.5 text-sm text-muted-foreground">
                    {t(
                      'account_subtitle',
                      'Manage passkeys and the account lifecycle.',
                    )}
                  </p>
                </header>
                <PasskeySecuritySection />
                <DeleteAccountCard />
              </TabsContent>
          </div>
        </Tabs>
      </div>
    </div>
  );
}
