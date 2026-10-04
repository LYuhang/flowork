/**
 * Shared route module loaders.
 *
 * React Router and the navigation shell both use these exact functions. The
 * router turns them into lazy components; the sidebar invokes them on
 * hover/focus so a deliberate navigation can use the browser module cache
 * instead of starting its network waterfall after the click.
 */
export const loadCanvasPage = () => import('@/pages/canvas/CanvasPage');
export const loadAppLayout = () => import('@/app/AppLayout');
export const loadChatPage = () => import('@/pages/chat/ChatPage');
export const loadEmbedChatPage = () => import('@/pages/embed/EmbedChatPage');
export const loadStandalonePreviewPage = () => import('@/pages/preview/StandalonePreviewPage');
export const loadPermissionsPage = () => import('@/pages/settings/PermissionsPage');
export const loadSettingsPage = () => import('@/pages/settings/SettingsPage');
export const loadLoginPage = () => import('@/pages/auth/LoginPage');
export const loadSignupPage = () => import('@/pages/auth/SignupPage');
export const loadResetPasswordPage = () => import('@/pages/auth/ResetPasswordPage');
export const loadWorkspacePage = () => import('@/pages/workspace/WorkspacePage');
export const loadTasksListPage = () => import('@/pages/tasks/TasksListPage');
export const loadTaskDetailPage = () => import('@/pages/tasks/TaskDetailPage');
export const loadDeploymentsListPage = () => import('@/pages/deployments/DeploymentsListPage');
export const loadDeploymentDetailPage = () => import('@/pages/deployments/DeploymentDetailPage');
export const loadMcpServersPage = () => import('@/pages/mcp-servers/McpServersPage');
export const loadMcpServerDetailPage = () => import('@/pages/mcp-servers/McpServerDetailPage');
export const loadMcpCatalogDetailPage = () => import('@/pages/mcp-servers/McpCatalogDetailPage');
export const loadSkillsPage = () => import('@/pages/skills/SkillsPage');
export const loadSkillDetailPage = () => import('@/pages/skills/SkillDetailPage');
export const loadSkillCatalogDetailPage = () => import('@/pages/skills/SkillCatalogDetailPage');
export const loadStoragePage = () => import('@/pages/storage/StoragePage');
export const loadKnowledgeListPage = () => import('@/pages/knowledge/KnowledgeListPage');
export const loadKnowledgeDetailPage = () => import('@/pages/knowledge/KnowledgeDetailPage');
export const loadPlatformManagementPage = () => import('@/pages/management/PlatformManagementPage');

const NAV_ROUTE_LOADERS: Readonly<Record<string, () => Promise<unknown>>> = {
  '/chat': loadChatPage,
  '/preview': loadStandalonePreviewPage,
  '/workspace': loadWorkspacePage,
  '/tasks': loadTasksListPage,
  '/tasks/:taskId': loadTaskDetailPage,
  '/deployments': loadDeploymentsListPage,
  '/deployments/:depId': loadDeploymentDetailPage,
  '/mcp-servers': loadMcpServersPage,
  '/mcp-servers/:id': loadMcpServerDetailPage,
  '/mcp-servers/discover/:source': loadMcpCatalogDetailPage,
  '/skills': loadSkillsPage,
  '/skills/:id': loadSkillDetailPage,
  '/skills/discover/:source': loadSkillCatalogDetailPage,
  '/storage': loadStoragePage,
  '/knowledge': loadKnowledgeListPage,
  '/knowledge/:kbId': loadKnowledgeDetailPage,
  '/workflow/:wfId': loadCanvasPage,
  '/settings': loadSettingsPage,
  '/permissions': loadPermissionsPage,
  '/management': loadPlatformManagementPage,
};

const pendingPreloads = new Map<string, Promise<unknown>>();

/** Preload a top-level route after the user signals navigation intent. */
export function preloadRoute(pathname: string): Promise<unknown> | undefined {
  const loader = NAV_ROUTE_LOADERS[pathname];
  if (!loader) return undefined;
  const existing = pendingPreloads.get(pathname);
  if (existing) return existing;

  const pending = loader().catch(() => {
    // A transient chunk/network failure must remain retryable on click.
    pendingPreloads.delete(pathname);
  });
  pendingPreloads.set(pathname, pending);
  return pending;
}
