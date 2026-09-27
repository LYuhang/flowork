/* eslint-disable react-refresh/only-export-components --
 * Router config files necessarily mix the exported `router` const with the
 * `lazy(...)` component bindings and the Suspense fallback. None of these
 * benefit from Fast Refresh (the router is created once at startup); the
 * rule is a Vite/HMR hint that doesn't apply here.
 */
/**
 * Browser router definition.
 *
 * The workspace route provides workflow listing and CRUD operations. Canvas
 * routes expose `/workflow/:wfId` for the live draft and
 * `/workflow/:wfId/version/:vKey` for a pinned read-only version.
 *
 * Every route page is code-split. This keeps Chat renderers, management,
 * authentication, canvas/editor, and file tooling out of unrelated entries
 * while retaining geometry-stable fallbacks.
 *
 * Business routes are wrapped in `<RequireAuth>`, and three public routes
 * (`/login`, `/signup`, `/reset-password`) sit beside
 * them at the top level. The auth pages have their own self-contained
 * `AuthLayout` and intentionally do NOT mount inside `<AppLayout>` — they
 * should not show the sidebar, the topbar, or the agent chat surface.
 *
 * NOTE: import path is `react-router` (v7), not `react-router-dom`. v7 merged
 * the two packages — see https://reactrouter.com/upgrading/v6#v7-changes.
 */
import { lazy, Suspense, type LazyExoticComponent, type ComponentType } from 'react';
import { createBrowserRouter, Navigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { getBasePath } from '@/lib/base-path';
import { RequireAuth } from '@/components/auth/RequireAuth';
import { Skeleton } from '@/components/ui/skeleton';
import {
  loadAppLayout,
  loadCanvasPage,
  loadChatPage,
  loadDeploymentDetailPage,
  loadDeploymentsListPage,
  loadEmbedChatPage,
  loadLoginPage,
  loadKnowledgeDetailPage,
  loadKnowledgeListPage,
  loadMcpCatalogDetailPage,
  loadMcpServerDetailPage,
  loadMcpServersPage,
  loadPlatformManagementPage,
  loadResetPasswordPage,
  loadSettingsPage,
  loadSignupPage,
  loadStandalonePreviewPage,
  loadSkillCatalogDetailPage,
  loadSkillDetailPage,
  loadSkillsPage,
  loadStoragePage,
  loadTaskDetailPage,
  loadTasksListPage,
  loadWorkspacePage,
} from '@/app/route-loaders';

// `React.lazy` requires a default export, but our pages use named exports
// for clarity (so the build catches typos in import sites). Re-shape the
// module here rather than touching every consumer.
const CanvasPage = lazy(() =>
  loadCanvasPage().then((m) => ({ default: m.CanvasPage })),
);
const AppLayout = lazy(() =>
  loadAppLayout().then((m) => ({ default: m.AppLayout })),
);
const ChatPage = lazy(() =>
  loadChatPage().then((m) => ({ default: m.ChatPage })),
);
const SharedChatPage = lazy(() =>
  import('@/pages/chat/SharedChatPage').then((m) => ({ default: m.SharedChatPage })),
);
const EmbedChatPage = lazy(() =>
  loadEmbedChatPage().then((m) => ({ default: m.EmbedChatPage })),
);
const StandalonePreviewPage = lazy(() =>
  loadStandalonePreviewPage().then((m) => ({ default: m.StandalonePreviewPage })),
);
const SettingsPage = lazy(() =>
  loadSettingsPage().then((m) => ({
    default: m.SettingsPage,
  })),
);
const LoginPage = lazy(() =>
  loadLoginPage().then((m) => ({ default: m.LoginPage })));
const SignupPage = lazy(() =>
  loadSignupPage().then((m) => ({ default: m.SignupPage })));
const ResetPasswordPage = lazy(() =>
  loadResetPasswordPage().then((m) => ({ default: m.ResetPasswordPage })));
const WorkspacePage = lazy(() =>
  loadWorkspacePage().then((m) => ({ default: m.WorkspacePage })));
const TasksListPage = lazy(() =>
  loadTasksListPage().then((m) => ({ default: m.TasksListPage })));
const TaskDetailPage = lazy(() =>
  loadTaskDetailPage().then((m) => ({ default: m.TaskDetailPage })));
const DeploymentsListPage = lazy(() =>
  loadDeploymentsListPage().then((m) => ({ default: m.DeploymentsListPage })));
const DeploymentDetailPage = lazy(() =>
  loadDeploymentDetailPage().then((m) => ({ default: m.DeploymentDetailPage })));
const McpServersPage = lazy(() =>
  loadMcpServersPage().then((m) => ({ default: m.McpServersPage })));
const McpServerDetailPage = lazy(() =>
  loadMcpServerDetailPage().then((m) => ({ default: m.McpServerDetailPage })));
const McpCatalogDetailPage = lazy(() =>
  loadMcpCatalogDetailPage().then((m) => ({ default: m.McpCatalogDetailPage })));
const SkillsPage = lazy(() =>
  loadSkillsPage().then((m) => ({ default: m.SkillsPage })));
const SkillDetailPage = lazy(() =>
  loadSkillDetailPage().then((m) => ({ default: m.SkillDetailPage })));
const SkillCatalogDetailPage = lazy(() =>
  loadSkillCatalogDetailPage().then((m) => ({ default: m.SkillCatalogDetailPage })));
const StoragePage = lazy(() =>
  loadStoragePage().then((m) => ({ default: m.StoragePage })));
const KnowledgeListPage = lazy(() =>
  loadKnowledgeListPage().then((m) => ({ default: m.KnowledgeListPage })));
const KnowledgeDetailPage = lazy(() =>
  loadKnowledgeDetailPage().then((m) => ({ default: m.KnowledgeDetailPage })));
const PlatformManagementPage = lazy(() =>
  loadPlatformManagementPage().then((m) => ({ default: m.PlatformManagementPage })));

function RouteFallback() {
  const { t } = useTranslation();
  return (
    <div
      className="flex h-full min-h-0 flex-1 flex-col bg-surface-work"
      aria-busy="true"
      role="status"
    >
      <span className="sr-only">{t('common.loading', 'Loading')}</span>
      <div className="flex h-14 shrink-0 items-center gap-3 border-b border-edge-structural px-5">
        <Skeleton className="h-5 w-40" />
        <Skeleton className="ml-auto h-8 w-24" />
      </div>
      <div className="min-h-0 flex-1 space-y-4 p-6">
        <Skeleton className="h-8 w-64 max-w-[55%]" />
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-[min(50vh,28rem)] w-full" />
      </div>
    </div>
  );
}

function AuthRouteFallback() {
  const { t } = useTranslation();
  return (
    <main
      className="grid min-h-screen place-items-center bg-surface-app px-6"
      aria-busy="true"
    >
      <span className="sr-only">{t('common.loading', 'Loading')}</span>
      <div className="w-full max-w-md space-y-6 rounded-xl border border-edge-structural bg-surface-work p-8">
        <Skeleton className="mx-auto h-10 w-40" />
        <div className="space-y-3">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      </div>
    </main>
  );
}

function routeElement(
  Page: LazyExoticComponent<ComponentType>,
  fallback = <RouteFallback />,
) {
  return (
    <Suspense fallback={fallback}>
      <Page />
    </Suspense>
  );
}

export const router = createBrowserRouter([
  // Side-panel embed renders only AgentChatSidebar for framing.
  // inside the extension side panel. Top level, OUTSIDE AppLayout (no app
  // chrome) and outside RequireAuth (the page seeds a relayed token or shows
  // its own login — Entry A/B in the design). It self-gates on auth.
  { path: '/embed/chat', element: routeElement(EmbedChatPage) },
  { path: '/share/:token', element: routeElement(SharedChatPage) },
  // Public auth pages — no AppLayout, no RequireAuth.
  { path: '/login', element: routeElement(LoginPage, <AuthRouteFallback />) },
  { path: '/signup', element: routeElement(SignupPage, <AuthRouteFallback />) },
  { path: '/reset-password', element: routeElement(ResetPasswordPage, <AuthRouteFallback />) },
  // Authenticated app shell.
  {
    path: '/',
    element: <RequireAuth />,
    children: [
      {
        path: 'preview',
        element: routeElement(StandalonePreviewPage),
      },
      {
        element: routeElement(AppLayout),
        children: [
          {
            index: true,
            element: <Navigate to="/chat" replace />,
          },
          {
            path: 'chat',
            element: routeElement(ChatPage),
          },
          {
            path: 'workspace',
            element: routeElement(WorkspacePage),
          },
          {
            path: 'management',
            element: routeElement(PlatformManagementPage),
          },
          {
            // Tenant-scoped task management list.
            // Route-split with a geometry-stable list fallback.
            path: 'tasks',
            element: routeElement(TasksListPage),
          },
          {
            // Per-task detail and live SSE event log.
            // Route-split with a stable detail-shell fallback.
            path: 'tasks/:taskId',
            element: routeElement(TaskDetailPage),
          },
          {
            // Tenant-scoped deployment management.
            // Route-split so charting and deployment controls load on demand.
            path: 'deployments',
            element: routeElement(DeploymentsListPage),
          },
          {
            path: 'deployments/:depId',
            element: routeElement(DeploymentDetailPage),
          },
          {
            path: 'credentials',
            element: <Navigate to="/settings?tab=api-keys" replace />,
          },
          {
            // Tenant-scoped MCP server management.
            path: 'mcp-servers',
            element: routeElement(McpServersPage),
          },
          {
            path: 'mcp-servers/discover/:source',
            element: routeElement(McpCatalogDetailPage),
          },
          {
            // Per-server detail (read-only config and probed tools).
            // Reached by clicking a card body on the list page.
            path: 'mcp-servers/:id',
            element: routeElement(McpServerDetailPage),
          },
          {
            // Tenant-scoped Skills management.
            path: 'skills',
            element: routeElement(SkillsPage),
          },
          {
            path: 'skills/discover/:source',
            element: routeElement(SkillCatalogDetailPage),
          },
          {
            // Per-skill detail (read-only SKILL.md, tools, and files).
            // Reached by clicking a card body on the list page.
            path: 'skills/:id',
            element: routeElement(SkillDetailPage),
          },
          {
            path: 'storage',
            element: routeElement(StoragePage),
          },
          {
            path: 'knowledge',
            element: routeElement(KnowledgeListPage),
          },
          {
            path: 'knowledge/:kbId',
            element: routeElement(KnowledgeDetailPage),
          },
          {
            path: 'workflow/:wfId',
            element: routeElement(CanvasPage),
          },
          {
            path: 'workflow/:wfId/version/:vKey',
            element: routeElement(CanvasPage),
          },
          {
            // Per-device user preferences such as language and theme.
            path: 'settings',
            element: routeElement(SettingsPage),
          },
          {
            // OpenRouter appends only `code` reliably. The one-time PKCE
            // state therefore lives in this callback path, not in a callback
            // query string that the provider may replace.
            path: 'settings/openrouter/callback/:openrouterState',
            element: routeElement(SettingsPage),
          },
        ],
      },
    ],
  },
  // The SPA works behind a path-prefix proxy (the workspace jump-host serves
  // the app under a DYNAMIC prefix like `/pws<token>/` that changes per
  // session). `getBasePath()` recovers that mount path from the entry chunk's
  // URL; served at root (local dev) it returns '' → '/' (no prefix). Without
  // this, react-router sees `/pws.../` in the address bar, matches no route,
  // and throws "No route matches URL" (404).
], { basename: getBasePath() || '/' });
