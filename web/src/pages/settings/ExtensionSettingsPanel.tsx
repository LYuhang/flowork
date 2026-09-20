import { CheckCircle2, Download, ExternalLink, Puzzle, ShieldCheck } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { AppIcon } from '@/app/AppIcon';
import { Button } from '@/components/ui/button';
import { getBasePath } from '@/lib/base-path';

const ARCHIVE_NAME = 'flowork-extension.zip';

function extensionDownloadUrl(): string {
  const configured = import.meta.env.VITE_BROWSER_EXTENSION_DOWNLOAD_URL?.trim();
  if (configured) return configured;
  return `${getBasePath()}/downloads/${ARCHIVE_NAME}`;
}

export function ExtensionSettingsPanel() {
  const { t } = useTranslation();
  const downloadUrl = extensionDownloadUrl();
  const externalDownload = /^https?:\/\//i.test(downloadUrl)
    && (typeof window === 'undefined' || new URL(downloadUrl).origin !== window.location.origin);

  const installSteps = [
    t('settings_extension_step_download', 'Download the ZIP package, then extract it to a permanent folder.'),
    t('settings_extension_step_page', 'Open chrome://extensions and turn on Developer mode.'),
    t('settings_extension_step_load', 'Choose “Load unpacked” and select the extracted folder.'),
    t('settings_extension_step_pin', 'Pin Flowork in the Chrome toolbar, then open its side panel.'),
  ];

  return (
    <div className="space-y-6" data-testid="settings-extension-panel">
      <section className="overflow-hidden rounded-xl border border-edge-structural bg-surface-raised">
        <div className="flex flex-col gap-5 p-5 sm:flex-row sm:items-start">
          <AppIcon
            className="size-16 shrink-0 rounded-2xl object-cover shadow-sm ring-1 ring-edge-subtle"
            alt=""
          />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-base font-semibold">
                {t('settings_extension_chrome_name', 'Flowork for Chrome')}
              </h3>
              <span className="rounded-full border border-edge-subtle bg-surface-sunken px-2 py-0.5 text-xs text-muted-foreground">
                Chrome
              </span>
            </div>
            <p className="mt-1 max-w-[62ch] text-sm leading-6 text-muted-foreground">
              {t(
                'settings_extension_chrome_desc',
                'Use the Flowork browser assistant beside any page. It shares your account when the main app is signed in, or lets you sign in directly from the side panel.',
              )}
            </p>
            <div className="mt-4 flex flex-wrap gap-2">
              <Button asChild>
                <a
                  href={downloadUrl}
                  download={externalDownload ? undefined : ARCHIVE_NAME}
                  target={externalDownload ? '_blank' : undefined}
                  rel={externalDownload ? 'noreferrer' : undefined}
                  data-action="download-browser-extension"
                >
                  <Download className="size-4" aria-hidden="true" />
                  {t('settings_extension_download', 'Download extension')}
                  {externalDownload ? <ExternalLink className="size-3.5" aria-hidden="true" /> : null}
                </a>
              </Button>
              <span className="inline-flex min-h-9 items-center text-xs text-muted-foreground">
                {t('settings_extension_package_hint', 'ZIP · current deployment build')}
              </span>
            </div>
          </div>
        </div>

        <div className="grid border-t border-edge-subtle bg-surface-sunken/35 sm:grid-cols-3">
          {[
            t('settings_extension_feature_chat', 'Browser-only chat history'),
            t('settings_extension_feature_page', 'Current-page Agent actions'),
            t('settings_extension_feature_resume', 'Persistent side-panel sessions'),
          ].map((feature) => (
            <div key={feature} className="flex items-center gap-2 border-edge-subtle px-4 py-3 text-sm sm:border-r sm:last:border-r-0">
              <CheckCircle2 className="size-4 shrink-0 text-state-success" aria-hidden="true" />
              <span>{feature}</span>
            </div>
          ))}
        </div>
      </section>

      <section className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(16rem,.72fr)]">
        <div>
          <div className="flex items-center gap-2">
            <Puzzle className="size-4 text-muted-foreground" aria-hidden="true" />
            <h3 className="text-sm font-semibold">
              {t('settings_extension_install_title', 'Install in Chrome')}
            </h3>
          </div>
          <ol className="mt-3 space-y-3">
            {installSteps.map((step, index) => (
              <li key={step} className="flex gap-3 text-sm leading-6">
                <span className="grid size-6 shrink-0 place-items-center rounded-full bg-primary/10 text-xs font-semibold text-primary">
                  {index + 1}
                </span>
                <span>{step}</span>
              </li>
            ))}
          </ol>
          <p className="mt-4 rounded-lg border border-edge-subtle bg-surface-sunken/45 px-3 py-2.5 text-xs leading-5 text-muted-foreground">
            {t(
              'settings_extension_update_hint',
              'To update: download the latest ZIP, replace the extracted folder, then click Reload on chrome://extensions. Chrome cannot load the ZIP directly.',
            )}
          </p>
        </div>

        <aside className="rounded-xl border border-edge-subtle bg-surface-sunken/35 p-4">
          <div className="flex items-center gap-2">
            <ShieldCheck className="size-4 text-state-success" aria-hidden="true" />
            <h3 className="text-sm font-semibold">
              {t('settings_extension_permissions_title', 'Why Chrome asks for permissions')}
            </h3>
          </div>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            {t(
              'settings_extension_permissions_desc',
              'Side panel and storage keep the assistant available. Tabs and debugger permissions let the Agent inspect or operate only when browser control is started. Chrome always shows its native control indicator while debugging is active.',
            )}
          </p>
          <p className="mt-3 text-xs leading-5 text-content-tertiary">
            {t(
              'settings_extension_security_note',
              'The main Web Session is never copied into extension storage. Account reuse uses a short-lived, single-use exchange code.',
            )}
          </p>
        </aside>
      </section>
    </div>
  );
}
