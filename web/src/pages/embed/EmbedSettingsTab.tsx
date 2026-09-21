/**
 * Embedded side-panel settings tab.
 *
 *   1. Language toggle (§14.4) — the same zh/en `setLocale` the `/settings`
 *      page uses, but additionally RELAYED to the extension so the Dynamic
 *      Island (a standalone content script with its own bilingual string table)
 *      renders in the same language:
 *        window.parent.postMessage({ type: 'SET_LANG', lang }, extensionOrigin).
 *      The current locale is reflected via `i18n.resolvedLanguage`.
 *
 *   2. Theme — reuses the same ThemeToggle as the main application.
 *   3. Default Runtime — reuses the account Runtime settings API and deployment
 *      capability list. It affects new browser Chats only; current Chats keep
 *      their immutable Runtime binding.
 *
 * Narrow-panel friendly: a single scrollable column.
 */
import { useEffect, useState } from 'react';
import { ExternalLink, LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { ThemeToggle } from '@/components/shared/ThemeToggle';
import {
  getAgentRuntimeSettings,
  setDefaultAgentRuntime,
  type AgentRuntimeSettings,
  type AgentRuntimeType,
} from '@/lib/api/agent-runtime';
import { runtimeCapabilitiesPrefix } from '@/lib/api/queries/agent-runtime';
import { useQueryClient } from '@tanstack/react-query';
import { setLocale, type Locale } from '@/lib/i18n';
import { extensionOrigin } from '@/lib/extension';
import { getBasePath } from '@/lib/base-path';

interface LanguageOption {
  value: Locale;
  label: string;
}

const LANGUAGES: LanguageOption[] = [
  { value: 'zh', label: '中文' },
  { value: 'en', label: 'English' },
];

/**
 * Switch the web UI locale AND relay it to the extension shell so the island
 * matches. The relay is side-effect-only and guarded on being framed; in the
 * (non-framed) test/dev render of this page it is simply a no-op postMessage to
 * self that nothing listens for.
 */
function applyLocale(lng: Locale): void {
  setLocale(lng);
  try {
    if (typeof window !== 'undefined' && window.parent !== window) {
      const origin = extensionOrigin();
      if (origin) window.parent.postMessage({ type: 'SET_LANG', lang: lng }, origin);
    }
  } catch {
    // Relay is best-effort: a failure must never block the UI locale switch.
  }
}

export function EmbedSettingsTab() {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const active = (i18n.resolvedLanguage ?? i18n.language) as Locale;
  const [runtimeSettings, setRuntimeSettings] = useState<AgentRuntimeSettings | null>(null);
  const [runtimeBusy, setRuntimeBusy] = useState(false);
  const [runtimeError, setRuntimeError] = useState('');

  useEffect(() => {
    let mounted = true;
    void getAgentRuntimeSettings()
      .then((settings) => mounted && setRuntimeSettings(settings))
      .catch((error: unknown) => {
        if (mounted) setRuntimeError(error instanceof Error ? error.message : String(error));
      });
    return () => {
      mounted = false;
    };
  }, []);

  const updateRuntime = async (runtime: AgentRuntimeType) => {
    if (!runtimeSettings || runtimeSettings.default_runtime_type === runtime) return;
    setRuntimeBusy(true);
    setRuntimeError('');
    try {
      const next = await setDefaultAgentRuntime(runtime);
      setRuntimeSettings(next);
      await queryClient.invalidateQueries({ queryKey: runtimeCapabilitiesPrefix });
    } catch (error) {
      setRuntimeError(error instanceof Error ? error.message : String(error));
    } finally {
      setRuntimeBusy(false);
    }
  };

  const availableRuntimes = new Set(runtimeSettings?.available_runtime_types ?? []);
  const runtimeOptions = (runtimeSettings?.runtime_options ?? []).filter((option) =>
    availableRuntimes.has(option.id)
  );
  const mainRuntimeSettingsUrl = `${getBasePath()}/settings?tab=runtime`;

  return (
    <div className="h-full overflow-y-auto bg-surface-work p-3.5">
      <div className="mx-auto max-w-md space-y-3">
        <header className="px-1 pb-1">
          <h2 className="text-sm font-semibold">{t('embed.settings.title', 'Side panel settings')}</h2>
          <p className="mt-0.5 text-xs leading-5 text-muted-foreground">
            {t('embed.settings.subtitle', 'Preferences that affect browser conversations on this account.')}
          </p>
        </header>
        {/* Language (§14.4) */}
        <section className="rounded-xl border border-edge-subtle bg-surface-raised p-3.5" data-testid="embed-settings-language">
          <h3 className="text-sm font-medium">
            {t('settings_language', 'Language')}
          </h3>
          <p className="mt-0.5 text-xs leading-5 text-muted-foreground">
            {t('embed.settings.language_hint', 'Also updates browser control status shown on web pages.')}
          </p>
          <div className="mt-3 flex gap-2">
            {LANGUAGES.map((opt) => (
              <Button
                key={opt.value}
                variant={active === opt.value ? 'default' : 'outline'}
                size="sm"
                data-action={`set-locale-${opt.value}`}
                aria-pressed={active === opt.value}
                onClick={() => applyLocale(opt.value)}
              >
                {opt.label}
              </Button>
            ))}
          </div>
        </section>

        <section className="flex items-center justify-between gap-3 rounded-xl border border-edge-subtle bg-surface-raised p-3.5" data-testid="embed-settings-theme">
          <div className="min-w-0">
            <h3 className="text-sm font-medium">{t('settings_theme', 'Theme')}</h3>
            <p className="mt-0.5 text-xs leading-5 text-muted-foreground">
              {t('settings_theme_desc', 'Switch between light, dark, or follow your system preference.')}
            </p>
          </div>
          <ThemeToggle />
        </section>

        <section className="rounded-xl border border-edge-subtle bg-surface-raised p-3.5" data-testid="embed-settings-runtime">
          <h3 className="text-sm font-medium">
            {t('settings_runtime_default', 'Default Agent runtime')}
          </h3>
          <p className="mt-0.5 text-xs leading-5 text-muted-foreground">
            {t(
              'embed.settings.runtime_hint',
              'Used by new browser Chats. Existing Chats keep the Runtime they started with.',
            )}
          </p>
          <div className="mt-3">
            <Select
              value={runtimeSettings?.default_runtime_type}
              onValueChange={(value) => void updateRuntime(value as AgentRuntimeType)}
              disabled={!runtimeSettings || runtimeBusy || runtimeOptions.length <= 1}
            >
              <SelectTrigger className="w-full" aria-label={t('settings_runtime_default', 'Default Agent runtime')}>
                {runtimeBusy ? <LoaderCircle className="size-3.5 animate-spin" aria-hidden="true" /> : null}
                <SelectValue placeholder={t('settings_runtime_loading', 'Loading runtime…')} />
              </SelectTrigger>
              <SelectContent>
                {runtimeOptions.map((option) => (
                  <SelectItem key={option.id} value={option.id}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {runtimeError ? (
              <p className="mt-2 text-xs leading-5 text-state-danger" role="alert">{runtimeError}</p>
            ) : null}
          </div>
          <Button asChild variant="ghost" size="sm" className="mt-2 h-8 px-2 text-xs">
            <a href={mainRuntimeSettingsUrl} target="_blank" rel="noreferrer">
              {t('embed.settings.open_runtime', 'Manage Runtime connections in main app')}
              <ExternalLink className="size-3.5" aria-hidden="true" />
            </a>
          </Button>
        </section>
      </div>
    </div>
  );
}
