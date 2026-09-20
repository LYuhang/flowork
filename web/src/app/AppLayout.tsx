/**
 * Top-level routed layout.
 *
 * Owns the per-page error boundary so a render failure inside any route
 * does not unmount the global shell (and therefore does not lose the
 * CommandPalette state once mounted in T15).
 *
 * The former empty global brand topbar has been removed. Management routes
 * keep account and Settings access in the sidebar utilities; workflow routes
 * provide their own contextual WorkbenchHeader. Authentication still lives on
 * the dedicated public routes outside this layout.
 *
 * `<RequireAuth>` resolves the HttpOnly Session before this layout mounts.
 */
import { lazy, Suspense, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Outlet, useLocation, useMatches } from 'react-router';
import { AppSidebar } from '@/app/AppSidebar';
import { MobileAppHeader } from '@/app/MobileAppHeader';
import { ErrorBoundary } from '@/app/ErrorBoundary';
import { KeyboardShortcuts } from '@/app/KeyboardShortcuts';
import { StepUpDialog } from '@/components/auth/StepUpDialog';
import { PrivilegedSupportBanner } from '@/components/auth/PrivilegedSupportBanner';
import { CanvasViewportProvider } from '@/pages/canvas/CanvasViewportContext';
import { useAuthStore } from '@/stores/auth';
import { useUIStore } from '@/stores/ui';
import { getAgentRuntimeSettings } from '@/lib/api/agent-runtime';
import { setTimezone } from '@/lib/timezone';
import { scheduleAuthenticatedRoutePreloads } from '@/app/route-loaders';

const CommandPalette = lazy(() =>
  import('@/components/command-palette/CommandPalette').then((module) => ({
    default: module.CommandPalette,
  })),
);
const VfsExplorer = lazy(() =>
  import('@/pages/canvas/explorer/VfsExplorer').then((module) => ({
    default: module.VfsExplorer,
  })),
);

export function AppLayout() {
  const { t } = useTranslation();
  const explorerOpen = useUIStore((s) => s.explorerOpen);
  const commandPaletteOpen = useUIStore((s) => s.commandPaletteOpen);
  const organizationSwitching = useAuthStore((s) => s.organizationSwitching);
  const userId = useAuthStore((s) => s.user?.user_id);
  const location = useLocation();
  const mainRef = useRef<HTMLElement>(null);
  const previousPathRef = useRef(location.pathname);

  useEffect(() => {
    if (!userId) return;
    let active = true;
    void getAgentRuntimeSettings()
      .then((settings) => {
        if (active && settings.preferred_timezone) {
          setTimezone(settings.preferred_timezone);
        }
      })
      .catch(() => {
        // The browser-detected/local preference remains a safe first-Turn
        // fallback; authenticated API errors are surfaced by their owner.
      });
    return () => {
      active = false;
    };
  }, [userId]);

  useEffect(() => {
    if (!userId) return;
    return scheduleAuthenticatedRoutePreloads(location.pathname);
    // This is a once-per-authenticated-shell warmup. Route intent continues
    // to use the higher-priority per-link preload path in AppSidebar.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId]);

  useEffect(() => {
    const segments = location.pathname.split('/').filter(Boolean);
    const section = segments[0] ?? 'chat';
    const labels: Record<string, string> = {
      chat: t('nav.chat', 'Chat'),
      workspace: t('nav.workflows', 'Workflows'),
      workflow: t('nav.workflow', 'Workflow'),
      management: t('nav.management', 'Management'),
      tasks: t('nav.tasks', 'Tasks'),
      deployments: t('nav.deployments', 'Deployments'),
      credentials: t('nav.credentials', 'API Keys'),
      'mcp-servers': t('nav.mcp', 'MCP Servers'),
      skills: t('nav.skills', 'Skills'),
      knowledge: t('nav.knowledge', 'Knowledge'),
      storage: t('nav.storage', 'Storage'),
      settings: t('nav.settings', 'Settings'),
    };
    const entityId = segments.length > 1 && !['workspace', 'chat', 'settings'].includes(section)
      ? segments[1]?.slice(0, 8)
      : '';
    const page = labels[section] ?? t('ws_title', 'Flowork');
    document.title = entityId ? `${page} ${entityId} · Flowork` : `${page} · Flowork`;
  }, [location.pathname, t]);

  useEffect(() => {
    if (previousPathRef.current === location.pathname) return;
    previousPathRef.current = location.pathname;
    const frame = window.requestAnimationFrame(() => {
      mainRef.current?.focus({ preventScroll: true });
      // Each route owns its internal scroll regions. Reset them together with
      // focus so a list/detail transition never inherits an unrelated vertical
      // position from the previous screen.
      if (mainRef.current) {
        mainRef.current.scrollTop = 0;
        mainRef.current
          .querySelectorAll<HTMLElement>('.page-shell, .page-scroll-region')
          .forEach((region) => { region.scrollTop = 0; });
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [location.pathname]);

  // Derive the active workflow + version pin from the ROUTER, not from the
  // store (`lastActiveWorkflowId` is never cleared → it would leak the shell
  // onto /workspace). `useMatches()` is route-synchronous (no
  // effect lag → no flash) and — unlike `useParams()` in a *parent* layout
  // route — reliably exposes the deepest child route's params. We scan every
  // match for the `:wfId` / `:vKey` segments of `/workflow/:wfId[/version/:vKey]`.
  const matches = useMatches();
  const routeParams = matches.reduce<{ wfId?: string; vKey?: string }>(
    (acc, m) => {
      const p = m.params as { wfId?: string; vKey?: string };
      if (p.wfId) acc.wfId = p.wfId;
      if (p.vKey) acc.vKey = p.vKey;
      return acc;
    },
    {},
  );
  const routeWfId = routeParams.wfId;
  const routeReadOnly = !!routeParams.vKey;

  return (
    <ErrorBoundary scope="page">
      <div className="surface-shell flex h-screen w-screen flex-col overflow-hidden text-foreground">
        <a
          href="#main-content"
          className="sr-only z-toast rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground focus:not-sr-only focus:fixed focus:left-3 focus:top-3"
        >
          {t('a11y.skipMain', 'Skip to main content')}
        </a>
        {/*
         * Global shortcut surface (returns null). Mounted once here so a
         * single window keydown listener serves every route — Cmd+K to
         * open the palette, Cmd+S to save, Cmd+Z/Shift+Z for workflow
         * undo/redo, Esc to close the palette.
         */}
        <KeyboardShortcuts />
        <StepUpDialog />
        <PrivilegedSupportBanner />
        {/*
         * Cmd+K palette. Reads its open flag from useUIStore so any
         * code path (shortcut, toolbar button, future tour) can pop it
         * by toggling a single store slice.
         */}
        {commandPaletteOpen && (
          <Suspense fallback={null}>
            <CommandPalette />
          </Suspense>
        )}
        {/*
         * The viewport-center provider wraps BOTH the Explorer palette and the
         * <Outlet> (which renders CanvasPage → Canvas). Canvas registers its
         * `screenToFlowPosition`-backed center getter here; the Explorer's
         * node cards (siblings of the canvas, NOT descendants) read it
         * for double-click-to-insert. Provided at this common ancestor so the
         * palette no longer falls back to the flow origin (the getter was
         * previously scoped inside CanvasPage, below this shell).
         */}
        <CanvasViewportProvider>
        <div className="flex min-h-0 flex-1 overflow-hidden">
          {/*
           * Left-MOST slot — the top-level management nav sidebar (Workflows /
           * Tasks / Deployments). MUTUALLY EXCLUSIVE with being inside a
           * workflow: it renders ONLY when there is NO `routeWfId`, so the
           * management shell is [AppSidebar | main] and a workflow is the
           * existing [Explorer? | main | Inspector] with no AppSidebar.
           * This prevents double-rendering two left sidebars.
           */}
          {!organizationSwitching && !routeWfId && <AppSidebar />}
          {/*
           * Left slot — the VFS Explorer, a top-level peer of the canvas.
           * Gated on the ROUTE (`routeWfId`),
           * so it renders only on `/workflow/:wfId` and never leaks onto
           * /workspace etc. Visibility is the `explorerOpen` collapse toggle
           * (driven by CanvasToolbar's "Files" button via the shared store).
           * wfId/readOnly come from the route; activeMajor/activeSub are
           * self-fetched inside VfsExplorer.
           */}
          {!organizationSwitching && routeWfId && explorerOpen && (
            <Suspense
              fallback={<div className="w-72 shrink-0 border-r border-edge-structural bg-surface-nav" />}
            >
              <VfsExplorer wfId={routeWfId} readOnly={routeReadOnly} vKey={routeParams.vKey} />
            </Suspense>
          )}
          <main ref={mainRef} id="main-content" tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden outline-none">
            {!organizationSwitching && !routeWfId ? <MobileAppHeader /> : null}
            {organizationSwitching ? (
              <div
                role="status"
                aria-live="polite"
                data-testid="organization-switch-pending"
                className="flex min-h-0 flex-1 items-center justify-center text-sm text-muted-foreground"
              >
                {t('organization.switching', 'Switching workspace…')}
              </div>
            ) : (
              <div key={location.pathname} className="route-transition flex min-h-0 min-w-0 flex-1 flex-col">
                <Outlet />
              </div>
            )}
          </main>
        </div>
        </CanvasViewportProvider>
      </div>
    </ErrorBoundary>
  );
}
