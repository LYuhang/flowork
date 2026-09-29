/**
 * Side-panel shell for embedded chat.
 *
 * Rendered AFTER the embed has bound a browser chat context. Browser side-panel
 * V1 exposes Chat plus a deliberately small Settings surface; workflow
 * execution remains a main-app surface.
 *
 *   - Chat       — the existing `AgentChatSidebar` (embedded variant).
 *   - Execution  — non-browser embed only.
 *   - Settings   — shared by browser and non-browser embeds.
 *
 * Non-browser embed tabs are kept MOUNTED (CSS-hidden when inactive) rather than
 * unmounted on switch, so live SSE streams and in-flight runs survive tab
 * changes.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, MessageSquare, Play, Settings } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { AgentChatSidebar } from '@/components/agent-sidebar/AgentChatSidebar';
import { EmbedExecutionTab } from '@/pages/embed/EmbedExecutionTab';
import { EmbedSettingsTab } from '@/pages/embed/EmbedSettingsTab';
import { cn } from '@/lib/utils';

type EmbedTab = 'chat' | 'execution' | 'settings';

export interface EmbedShellProps {
  /** The bound workflow id (always present once the shell renders). */
  wfId: string;
  /** Composer default mode for the Chat tab. */
  defaultMode: 'chat' | 'browser';
  browserControlChatId?: string;
  browserControlAvailableHere?: boolean;
  /** Browser-only side panel hides workflow execution surfaces. */
  browserOnly?: boolean;
  transportConnected?: boolean | null;
}

export function EmbedShell({
  wfId,
  defaultMode,
  browserControlChatId,
  browserControlAvailableHere,
  browserOnly = false,
  transportConnected,
}: EmbedShellProps) {
  const { t } = useTranslation();
  const [tab, setTab] = useState<EmbedTab>('chat');
  const [settingsVisited, setSettingsVisited] = useState(false);
  const selectTab = (next: EmbedTab) => {
    if (next === 'settings') setSettingsVisited(true);
    setTab(next);
  };

  const tabs: Array<{ id: EmbedTab; label: string; icon: typeof MessageSquare }> = [
    { id: 'chat', label: t('embed.tab.chat', 'Chat'), icon: MessageSquare },
    ...(browserOnly
      ? []
      : [{
          id: 'execution' as const,
          label: t('embed.tab.execution', 'Execution'),
          icon: Play,
        }]),
    {
      id: 'settings' as const,
      label: t('embed.tab.settings', 'Settings'),
      icon: Settings,
    },
  ];

  return (
    <div className="flex h-full w-full min-w-0 flex-col overflow-hidden bg-surface-work text-foreground">
      {browserOnly && tab === 'settings' && (
        <header className="flex h-14 shrink-0 items-center gap-2 px-3">
          <Button variant="ghost" size="icon" className="rounded-full" onClick={() => selectTab('chat')} aria-label={t('embed.back_to_chat', 'Back to chat')}>
            <ArrowLeft className="size-4" />
          </Button>
          <h1 className="text-sm font-semibold">{t('embed.tab.settings', 'Settings')}</h1>
        </header>
      )}
      {!browserOnly && tabs.length > 1 && (
        <div
          role="tablist"
          aria-label={t('embed.tab.aria', 'Side panel sections')}
          className="flex shrink-0 items-center gap-1 border-b border-edge-structural bg-surface-nav p-1.5"
        >
          {tabs.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={tab === id}
              data-action={`embed-tab-${id}`}
              onClick={() => selectTab(id)}
              className={cn(
                'flex h-8 min-w-0 flex-1 items-center justify-center gap-1.5 rounded-lg px-2 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/40',
                tab === id
                  ? 'bg-surface-raised text-foreground'
                  : 'text-muted-foreground hover:bg-surface-hover hover:text-foreground',
              )}
            >
              <Icon className="h-3.5 w-3.5 shrink-0" />
              <span className="truncate">{label}</span>
            </button>
          ))}
        </div>
      )}

      {/* Bodies stay mounted; inactive ones are CSS-hidden to preserve live
          state (SSE stream / in-flight run) and scroll position across switches. */}
      <div className="relative min-h-0 flex-1">
        <div
          className={cn(
            'absolute inset-0 flex-col',
            // `flex` (NOT `block`) when active — `block` would override the base
            // display and collapse the flex column, floating the composer up.
            tab === 'chat' ? 'flex' : 'hidden',
          )}
        >
          <div className="flex min-h-0 flex-1 flex-col">
            <AgentChatSidebar
              embedded
              defaultMode={defaultMode}
              browserControlChatId={browserControlChatId}
              browserControlAvailableHere={browserControlAvailableHere}
              browserTransportConnected={transportConnected}
              chatSurface="browser"
              showEmbeddedSettingsButton={browserOnly}
              onOpenEmbeddedSettings={() => selectTab('settings')}
              showComposerModelSelector
              active={tab === 'chat'}
            />
          </div>
        </div>
        {!browserOnly && (
          <div
            className={cn('absolute inset-0', tab === 'execution' ? 'block' : 'hidden')}
          >
            <EmbedExecutionTab wfId={wfId} />
          </div>
        )}
        <div
          className={cn('absolute inset-0', tab === 'settings' ? 'block' : 'hidden')}
        >
          {settingsVisited && <EmbedSettingsTab />}
        </div>
      </div>
    </div>
  );
}
