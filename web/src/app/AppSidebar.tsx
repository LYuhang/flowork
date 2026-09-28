/**
 * Left navigation sidebar for the top-level MANAGEMENT shell (TASK B).
 *
 * Replaces the old top-header tabs (Workflows / Tasks / Deployments). Rendered
 * by AppLayout ONLY on management routes (no `routeWfId`) — it is completely
 * absent inside a workflow editor, where the existing Explorer / AgentChat
 * sidebars own the left/right slots.
 *
 * Collapsible: the toggle flips `navSidebarCollapsed` in the shared UI store.
 *   - expanded  → w-56, icon + label per item.
 *   - collapsed → w-14, icon-only rail.
 *
 * The app wordmark and account utilities live here because the global empty
 * brand header has been removed from the management shell.
 */
import { useTranslation } from 'react-i18next';
import { openProjectChatDraft } from '@/lib/chat/project-draft';
import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { NavLink, useLocation, useNavigate } from 'react-router';
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Folder,
  FolderPlus,
  Plus,
  Pencil,
  Trash2,
} from 'lucide-react';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
import { useUIStore } from '@/stores/ui';
import { useChatStreamStore } from '@/stores/chat-stream';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from '@/components/ui/context-menu';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Skeleton } from '@/components/ui/skeleton';
import { StatusDot } from '@/components/ui/status';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import {
  ChatDeleteError,
  CHAT_HISTORY_GC_TIME_MS,
  CHAT_HISTORY_STALE_TIME_MS,
  useDeleteChatSession,
  useDeleteChatProject,
  useCreateChatProject,
  useRenameChatProject,
  useRenameChatSession,
  fetchChatHistory,
  useProjectSandboxStatuses,
  useProjectSandboxAction,
  useChatSessions,
  useChatProjects,
  useGeneralChatBootstrap,
} from '@/lib/api/queries/chats';
import { queryClient } from '@/app/query-client';
import { preloadRoute } from '@/app/route-loaders';
import { AppLogo } from '@/app/AppLogo';
import { AppIcon } from '@/app/AppIcon';
import { OrganizationSwitcher } from '@/components/shared/OrganizationSwitcher';
import { UserMenuDropdown } from '@/components/shared/UserMenuDropdown';
import { listOrganizations } from '@/lib/api/organizations';
import { organizationsQueryKey } from '@/lib/api/organization-query-keys';
import { useAuthStore } from '@/stores/auth';
import { ResourceIcon } from '@/components/presentation/ResourceIcon';
import type { ResourceKind } from '@/lib/presentation/resource-visuals';
import type { SandboxLifecycleStatus } from '@/lib/sandbox-status';

type SidebarChatItem = {
  chat_id: string;
  project_id?: string | null;
  chat_context: string;
  surface?: 'chat' | 'browser';
};

interface NavItem {
  to: string;
  kind: ResourceKind;
  /** i18n key (REUSES the existing top-nav label keys). */
  labelKey: string;
  fallback: string;
}

interface NavGroup {
  labelKey: string;
  fallback: string;
  items: NavItem[];
}

const NAV_GROUPS: NavGroup[] = [
  {
    labelKey: 'nav.group.build',
    fallback: 'Build',
    items: [
      { to: '/chat', kind: 'chat', labelKey: 'nav.project', fallback: 'Projects' },
      { to: '/workspace', kind: 'workflow', labelKey: 'nav.workspace', fallback: 'Workflow' },
    ],
  },
  {
    labelKey: 'nav.group.operate',
    fallback: 'Operate',
    items: [
      { to: '/tasks', kind: 'task', labelKey: 'nav.tasks', fallback: 'Task' },
      { to: '/deployments', kind: 'deployment', labelKey: 'nav.deployments', fallback: 'Deployment' },
    ],
  },
  {
    labelKey: 'nav.group.resources',
    fallback: 'Resources',
    items: [
      { to: '/mcp-servers', kind: 'mcp', labelKey: 'nav.mcpServers', fallback: 'MCP Server' },
      { to: '/skills', kind: 'skill', labelKey: 'nav.skills', fallback: 'Skill' },
      { to: '/knowledge', kind: 'knowledge', labelKey: 'nav.knowledge', fallback: 'Knowledge' },
      { to: '/storage', kind: 'storage', labelKey: 'nav.storage', fallback: 'Storage' },
    ],
  },
];

export function AppSidebar({
  mobile = false,
  onNavigate,
}: {
  mobile?: boolean;
  onNavigate?: () => void;
} = {}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const collapsed = useUIStore((s) => s.navSidebarCollapsed);
  const toggle = useUIStore((s) => s.toggleNavSidebar);
  const activeChatId = useUIStore((s) => s.activeChatIds.chat);
  const activeProjectId = useUIStore((s) => s.activeProjectId);
  const setActiveChatId = useUIStore((s) => s.setActiveChatId);
  const setActiveProjectId = useUIStore((s) => s.setActiveProjectId);
  const setChatEntryIntent = useUIStore((s) => s.setChatEntryIntent);
  const optimisticChatSessions = useUIStore((s) => s.optimisticChatSessions);
  const removeOptimisticChatSession = useUIStore((s) => s.removeOptimisticChatSession);
  const [deleteTarget, setDeleteTarget] = useState<{
    chat_id: string;
    label: string;
  } | null>(null);
  const [deleteChatFiles, setDeleteChatFiles] = useState(false);
  const [renameTarget, setRenameTarget] = useState<{
    chat_id: string;
    label: string;
  } | null>(null);
  const [renameDraft, setRenameDraft] = useState('');
  const [createProjectOpen, setCreateProjectOpen] = useState(false);
  const [createProjectName, setCreateProjectName] = useState('');
  const [renameProjectTarget, setRenameProjectTarget] = useState<{
    project_id: string;
    label: string;
  } | null>(null);
  const [renameProjectDraft, setRenameProjectDraft] = useState('');
  const [deleteProjectTarget, setDeleteProjectTarget] = useState<{
    project_id: string;
    label: string;
  } | null>(null);
  const [collapsedProjects, setCollapsedProjects] = useState<Set<string>>(() => new Set());
  const activeOrganizationId = useAuthStore((state) => state.user?.tenant_id ?? '');
  const organizations = useQuery({
    queryKey: organizationsQueryKey,
    queryFn: listOrganizations,
    enabled: Boolean(activeOrganizationId),
  });
  const activeOrganization = organizations.data?.items.find(
    (item) => item.organization_id === activeOrganizationId,
  );
  const organizationCapabilities = new Set(activeOrganization?.access.capabilities ?? []);
  const canManageOrganization = activeOrganization?.kind === 'business'
    && (
      organizationCapabilities.has('manage_members')
      || organizationCapabilities.has('manage_policy')
      || organizationCapabilities.has('view_audit')
    );
  const platformManagementRole = useAuthStore(
    (state) => state.user?.platformManagementRole ?? null,
  );
  const managementItems = useMemo<NavItem[]>(() => {
    const items: NavItem[] = [];
    if (platformManagementRole) {
      items.push({
        to: '/management',
        kind: 'management',
        labelKey: 'nav.platformManagement',
        fallback: 'Platform overview',
      });
    }
    if (canManageOrganization) {
      items.push({
        to: '/settings?tab=organization',
        kind: 'organization',
        labelKey: 'nav.organizationManagement',
        fallback: 'Organization',
      });
    }
    return items;
  }, [canManageOrganization, platformManagementRole]);
  const navGroups = useMemo(
    () => managementItems.length > 0
      ? [
          ...NAV_GROUPS,
          {
            labelKey: 'nav.group.management',
            fallback: 'Management',
            items: managementItems,
          },
        ]
      : NAV_GROUPS,
    [managementItems],
  );
  const effectiveCollapsed = mobile ? false : collapsed;
  const showChatContext = location.pathname === '/chat' && !effectiveCollapsed;
  const boot = useGeneralChatBootstrap();
  const carrierScopeId = boot.data?.carrier_scope_id ?? null;
  const sessions = useChatSessions(showChatContext ? carrierScopeId : null);
  const projects = useChatProjects(showChatContext);
  const createProject = useCreateChatProject();
  const renameProject = useRenameChatProject();
  const deleteProject = useDeleteChatProject();
  const deleteChat = useDeleteChatSession(carrierScopeId, 'chat', deleteChatFiles);
  const renameChat = useRenameChatSession(carrierScopeId, 'chat');
  const chatItems = useMemo<SidebarChatItem[]>(() => {
    const persistedRaw = (sessions.data?.items ?? []) as SidebarChatItem[];
    if (!carrierScopeId) return persistedRaw;
    const optimisticForScope = optimisticChatSessions.filter(
      (item) =>
        item.scopeId === carrierScopeId &&
        item.surface === 'chat',
    );
    const optimisticById = new Map(optimisticForScope.map((item) => [item.chat_id, item]));
    const persisted = persistedRaw.map((item) => {
      const optimistic = optimisticById.get(item.chat_id);
      const persistedTitle = (item.chat_context || '').trim().toLowerCase();
      if (optimistic && optimistic.chat_context && (!persistedTitle || persistedTitle === 'new chat')) {
        return { ...item, chat_context: optimistic.chat_context };
      }
      return item;
    });
    const persistedIds = new Set(persisted.map((item: SidebarChatItem) => item.chat_id));
    const optimistic: SidebarChatItem[] = optimisticForScope
      .filter((item) => !persistedIds.has(item.chat_id))
      .map((item) => ({
        chat_id: item.chat_id,
        project_id: item.projectId,
        chat_context: item.chat_context,
        surface: item.surface,
      }));
    return [...optimistic, ...persisted];
  }, [carrierScopeId, optimisticChatSessions, sessions.data?.items]);
  const sandboxStatuses = useProjectSandboxStatuses((projects.data ?? []).map((item) => item.project_id));
  const sandboxStatusByProject = new Map((sandboxStatuses.data?.items ?? []).map((item) => [item.project_id, item]));
  const sandboxAction = useProjectSandboxAction();
  const [releaseProject, setReleaseProject] = useState<{ project_id: string; name: string } | null>(null);
  const chatRuntimes = useChatStreamStore((state) => state.runtimes);
  const changeSandbox = async (projectId: string, action: 'start' | 'release') => {
    try {
      await sandboxAction.mutateAsync({ projectId, action });
      setReleaseProject(null);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t('error', 'Error'));
    }
  };
  const selectChat = (chatId: string, projectId?: string | null) => {
    setChatEntryIntent('select');
    if (projectId) setActiveProjectId(projectId);
    if (!carrierScopeId || chatId === activeChatId) {
      if (location.pathname !== '/chat') navigate('/chat');
      onNavigate?.();
      return;
    }
    // Switch the shell immediately. Transcript hydration belongs to the new
    // Chat page's loading region and must never block navigation.
    setActiveChatId('chat', chatId);
    if (location.pathname !== '/chat') navigate('/chat');
    onNavigate?.();
    preloadChat(chatId);
  };

  const newChat = (projectId: string) => {
    if (!carrierScopeId) return;
    openProjectChatDraft({
      account: useAuthStore.getState().user,
      projectId, scopeId: carrierScopeId,
      startedChatIds: chatItems.map((chat) => chat.chat_id),
    });
    setCollapsedProjects((current) => {
      if (!current.has(projectId)) return current;
      const next = new Set(current);
      next.delete(projectId);
      return next;
    });
    if (location.pathname !== '/chat') navigate('/chat');
    onNavigate?.();
  };

  const selectProject = (projectId: string) => {
    setCollapsedProjects((current) => {
      const next = new Set(current);
      if (activeProjectId !== projectId) next.delete(projectId);
      else if (next.has(projectId)) next.delete(projectId);
      else next.add(projectId);
      return next;
    });
    setActiveProjectId(projectId);
    if (activeProjectId !== projectId) {
      const latestChat = chatItems.find((item) => item.project_id === projectId);
      if (latestChat) selectChat(latestChat.chat_id, projectId);
      else {
        newChat(projectId);
      }
    }
  };

  const submitCreateProject = async () => {
    const name = createProjectName.trim().replace(/\s+/g, ' ');
    if (!name || !carrierScopeId) return;
    try {
      const project = await createProject.mutateAsync(name);
      setCreateProjectOpen(false);
      setCreateProjectName('');
      newChat(project.project_id);
      toast.success(t('nav.projects.created', 'Project created'));
    } catch {
      toast.error(t('nav.projects.createFailed', 'Could not create project'));
    }
  };

  const submitRenameProject = async () => {
    if (!renameProjectTarget) return;
    const name = renameProjectDraft.trim().replace(/\s+/g, ' ');
    if (!name || name === renameProjectTarget.label) return;
    try {
      await renameProject.mutateAsync({ projectId: renameProjectTarget.project_id, name });
      setRenameProjectTarget(null);
      toast.success(t('nav.projects.renamed', 'Project renamed'));
    } catch {
      toast.error(t('nav.projects.renameFailed', 'Could not rename project'));
    }
  };

  const submitDeleteProject = async () => {
    if (!deleteProjectTarget) return;
    try {
      const result = await deleteProject.mutateAsync(deleteProjectTarget.project_id);
      for (const chatId of result.deleted_chat_ids) {
        if (carrierScopeId) removeOptimisticChatSession(carrierScopeId, chatId);
      }
      if (activeProjectId === deleteProjectTarget.project_id) {
        setActiveProjectId(null);
        setActiveChatId('chat', null);
      }
      setDeleteProjectTarget(null);
      toast.success(t('nav.projects.deleted', 'Project deleted'));
    } catch {
      toast.error(t('nav.projects.deleteFailed', 'Could not delete project'));
    }
  };

  const preloadChat = (chatId: string) => {
    if (!carrierScopeId) return;
    void queryClient.prefetchQuery({
      queryKey: ['chat-history', carrierScopeId, chatId, null],
      queryFn: () => fetchChatHistory(carrierScopeId, chatId),
      staleTime: CHAT_HISTORY_STALE_TIME_MS,
      gcTime: CHAT_HISTORY_GC_TIME_MS,
    }).catch(() => undefined);
  };

  const submitDeleteChat = async () => {
    if (!carrierScopeId || !deleteTarget) return;
    const chatId = deleteTarget.chat_id;
    try {
      await deleteChat.mutateAsync(chatId);
      removeOptimisticChatSession(carrierScopeId, chatId);
      queryClient.removeQueries({ queryKey: ['chat-history', carrierScopeId, chatId] });
      queryClient.removeQueries({ queryKey: ['chat-workspace', chatId] });
      if (activeChatId === chatId) {
        setActiveChatId('chat', null);
        navigate('/chat');
      }
      setDeleteTarget(null);
      toast.success(t('nav.chatHistory.deleted', 'Chat deleted'));
    } catch (e) {
      toast.error(
        e instanceof ChatDeleteError
          ? e.code === 'browser_session_active'
            ? t(
                'nav.chatHistory.browserSessionActive',
                'End browser control before deleting this chat.',
              )
            : e.code === 'chat_turn_active'
              ? t(
                  'nav.chatHistory.turnActive',
                  'Stop the Agent before deleting this chat.',
                )
              : t('nav.chatHistory.deleteFailed', 'Delete failed')
          : t('nav.chatHistory.deleteFailed', 'Delete failed'),
      );
    }
  };

  const openRenameChat = (chatId: string, label: string) => {
    setRenameTarget({ chat_id: chatId, label });
    setRenameDraft(label);
  };

  const submitRenameChat = async () => {
    if (!renameTarget) return;
    const name = renameDraft.trim().replace(/\s+/g, ' ');
    if (!name || name === renameTarget.label) return;
    try {
      await renameChat.mutateAsync({ chatId: renameTarget.chat_id, name });
      setRenameTarget(null);
      toast.success(t('nav.chatHistory.renamed', 'Chat renamed'));
    } catch {
      toast.error(t('nav.chatHistory.renameFailed', 'Rename failed'));
    }
  };

  return (
    <>
      <nav
        data-testid="app-sidebar"
        aria-label={t('nav.primary', 'Primary')}
        className={cn(
          'surface-sidebar min-h-0 shrink-0 flex-col',
          mobile
            ? 'flex h-full w-full border-0'
            : 'hidden border-r md:flex',
          effectiveCollapsed ? 'w-16' : 'w-60',
        )}
      >
      <div
        className={cn(
          'flex h-[52px] shrink-0 items-center border-b border-edge-subtle px-3',
          effectiveCollapsed ? 'justify-center px-0' : 'justify-between',
        )}
      >
        {effectiveCollapsed ? (
          <span className="grid size-9 place-items-center" aria-label={t('ws_title', 'Flowork')}>
            <AppIcon
              alt=""
              aria-hidden="true"
              className="size-9 rounded-[10px]"
            />
          </span>
        ) : (
          <AppLogo />
        )}
      </div>
      <div className="shrink-0 px-2 py-2">
        {navGroups.map((group, groupIndex) => (
          <section
            key={group.labelKey}
            aria-labelledby={effectiveCollapsed ? undefined : `nav-group-${groupIndex}`}
            className={cn(groupIndex > 0 && 'mt-3 border-t border-edge-subtle pt-3')}
          >
            {!effectiveCollapsed ? (
              <h2
                id={`nav-group-${groupIndex}`}
                className="mb-1 px-3 text-[12px] font-semibold uppercase tracking-[0.08em] text-content-tertiary"
              >
                {t(group.labelKey, group.fallback)}
              </h2>
            ) : null}
            <ul className="space-y-0.5">
          {group.items.map((item) => {
            const label = t(item.labelKey, item.fallback);
            return (
              <li key={item.to}>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <NavLink
                      to={item.to}
                      aria-label={label}
                      onPointerEnter={() => preloadRoute(item.to)}
                      onFocus={() => preloadRoute(item.to)}
                      onClick={() => {
                        if (item.to === '/chat') setChatEntryIntent(null);
                        onNavigate?.();
                      }}
                      className={cn(
                        'text-ui relative flex h-9 items-center gap-3 rounded-md px-3 font-semibold text-muted-foreground transition-colors duration-feedback before:absolute before:inset-y-2 before:left-0 before:w-0.5 before:rounded-full before:bg-focus before:opacity-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring hover:bg-surface-hover/70 hover:text-foreground aria-[current=page]:bg-primary/[0.07] aria-[current=page]:font-bold aria-[current=page]:text-foreground aria-[current=page]:before:opacity-100',
                        effectiveCollapsed && 'justify-center px-0',
                      )}
                    >
                      <ResourceIcon kind={item.kind} size="sm" />
                      {!effectiveCollapsed && <span className="truncate">{label}</span>}
                    </NavLink>
                  </TooltipTrigger>
                  {effectiveCollapsed ? <TooltipContent side="right">{label}</TooltipContent> : null}
                </Tooltip>
              </li>
            );
          })}
            </ul>
          </section>
        ))}
      </div>
      {showChatContext ? (
        <div className="flex min-h-0 flex-1 flex-col border-t border-edge-subtle pt-3">
          <div className="flex items-center justify-between px-3">
            <span className="text-xs font-semibold text-muted-foreground">
              {t('nav.projects', 'Projects')}
            </span>
            <button
              type="button"
              className="grid size-7 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-surface-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onClick={() => setCreateProjectOpen(true)}
              aria-label={t('nav.projects.new', 'New project')}
              title={t('nav.projects.new', 'New project')}
            >
              <FolderPlus className="size-4" />
            </button>
          </div>
          <div className="app-scrollbar mt-2 min-h-0 flex-1 space-y-1 overflow-y-auto px-2 pb-2">
            {boot.isLoading || sessions.isLoading || projects.isLoading ? (
              <div className="space-y-1 px-3">
                <Skeleton className="h-7 w-full" />
                <Skeleton className="h-7 w-5/6" />
              </div>
            ) : projects.data?.length ? (
              projects.data.map((project) => {
                const projectChats = chatItems.filter(
                  (item) => item.project_id === project.project_id,
                );
                const expanded = !collapsedProjects.has(project.project_id);
                const sandboxState: SandboxLifecycleStatus = sandboxStatusByProject.get(project.project_id)?.status ?? 'idle';
                const sandboxBusy = sandboxAction.isPending && sandboxAction.variables?.projectId === project.project_id;
                const sandboxLabel = sandboxBusy
                  ? t('loading', 'Loading...')
                  : t(`chat.sandbox.${sandboxState}`);
                const sandboxTone = sandboxState === 'running' ? 'success'
                  : ['restoring', 'hibernating', 'releasing'].includes(sandboxState) ? 'running'
                  : sandboxState === 'snapshot_failed' ? 'danger' : 'neutral';
                const projectRow = (
                  <div className="group/project relative flex items-center">
                    <button
                      type="button"
                      className={cn(
                        'flex min-h-12 min-w-0 flex-1 items-center gap-2 rounded-md px-2 py-1.5 pr-8 text-left text-ui font-semibold transition-colors',
                        activeProjectId === project.project_id
                          ? 'bg-primary/[0.06] text-foreground'
                          : 'text-muted-foreground hover:bg-surface-hover/70 hover:text-foreground',
                      )}
                      aria-expanded={expanded}
                      onClick={() => selectProject(project.project_id)}
                    >
                      {expanded ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
                      <span className="relative shrink-0">
                        <Folder className="size-4" />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate">{project.name}</span>
                        <span className="mt-0.5 flex items-center gap-1.5 text-[11px] font-normal text-muted-foreground" title={sandboxLabel}>
                          <StatusDot status={sandboxTone} pulse={sandboxBusy || sandboxTone === 'running'} />
                          <span className="truncate">{project.runtime_type === 'codex' ? 'Codex' : project.runtime_type} · {sandboxLabel}</span>
                        </span>
                      </span>
                    </button>
                    <button
                      type="button"
                      className="absolute right-1 grid size-7 place-items-center rounded-md text-muted-foreground opacity-0 transition-opacity hover:bg-surface-raised hover:text-foreground focus-visible:opacity-100 group-hover/project:opacity-100"
                      onClick={(event) => {
                        event.stopPropagation();
                        newChat(project.project_id);
                      }}
                      aria-label={t('nav.projects.newChat', 'New chat in {{name}}', { name: project.name })}
                      disabled={!carrierScopeId}
                      title={t('nav.projects.newChat', 'New chat in {{name}}', { name: project.name })}
                    >
                      <Plus className="size-3.5" />
                    </button>
                  </div>
                );
                return (
                  <div key={project.project_id}>
                    <ContextMenu>
                      <ContextMenuTrigger asChild>{projectRow}</ContextMenuTrigger>
                      <ContextMenuContent className="w-48">
                        <ContextMenuItem disabled={!carrierScopeId} onSelect={() => newChat(project.project_id)}>
                          <Plus className="mr-2 size-4" />
                          {t('new_chat', 'New Chat')}
                        </ContextMenuItem>
                        <ContextMenuItem disabled={sandboxBusy} onSelect={() => {
                          if (sandboxState === 'idle' || sandboxState === 'closed') void changeSandbox(project.project_id, 'start');
                          else setReleaseProject(project);
                        }}>
                          {sandboxState === 'idle' || sandboxState === 'closed'
                            ? t('chat.sandbox.start_hint', 'Start sandbox')
                            : t('chat.sandbox.close_hint', 'Release sandbox')}
                        </ContextMenuItem>
                        <ContextMenuItem onSelect={() => {
                          setRenameProjectTarget({ project_id: project.project_id, label: project.name });
                          setRenameProjectDraft(project.name);
                        }}>
                          <Pencil className="mr-2 size-4" />
                          {t('nav.projects.rename', 'Rename project')}
                        </ContextMenuItem>
                        <ContextMenuSeparator />
                        <ContextMenuItem
                          className="text-destructive focus:text-destructive"
                          onSelect={() => setDeleteProjectTarget({
                            project_id: project.project_id,
                            label: project.name,
                          })}
                        >
                          <Trash2 className="mr-2 size-4" />
                          {t('nav.projects.delete', 'Delete project')}
                        </ContextMenuItem>
                      </ContextMenuContent>
                    </ContextMenu>
                    {expanded && projectChats.length > 0 ? (
                      <div className="ml-[15px] border-l border-edge-subtle pl-2">
                        {projectChats.map((item) => {
                          const label = item.chat_context || item.chat_id.slice(0, 8);
                          const chatRow = (
                            <div className="group/chat-history relative flex items-center">
                              <button
                                type="button"
                                title={label}
                                data-chat-id={item.chat_id}
                                onPointerEnter={() => preloadChat(item.chat_id)}
                                onFocus={() => preloadChat(item.chat_id)}
                                onClick={() => selectChat(item.chat_id, project.project_id)}
                                className={cn(
                                  'flex h-8 min-w-0 flex-1 items-center gap-2 rounded-md px-2 text-left text-[13px] transition-colors',
                                  item.chat_id === activeChatId
                                    ? 'bg-primary/[0.07] font-semibold text-foreground'
                                    : 'text-muted-foreground hover:bg-surface-hover/70 hover:text-foreground',
                                )}
                              >
                                <ResourceIcon kind="chat" size="sm" className="size-5 rounded" />
                                <span className="min-w-0 flex-1 truncate">{label}</span>
                                {chatRuntimes[item.chat_id]?.state === 'streaming' && (
                                  <StatusDot status="running" pulse title={t('chat.status.running', 'Running')} />
                                )}
                              </button>
                                <button
                                  type="button"
                                  className="absolute right-1 grid size-6 place-items-center rounded text-muted-foreground opacity-0 hover:bg-surface-raised hover:text-destructive focus-visible:opacity-100 group-hover/chat-history:opacity-100"
                                  aria-label={t('nav.chatHistory.delete', 'Delete')}
                                  onClick={(event) => {
                                    event.stopPropagation();
                                    setDeleteChatFiles(false);
                                    setDeleteTarget({ chat_id: item.chat_id, label });
                                  }}
                                >
                                  <Trash2 className="size-3.5" />
                                </button>
                            </div>
                          );
                          return (
                            <ContextMenu key={item.chat_id}>
                              <ContextMenuTrigger asChild>{chatRow}</ContextMenuTrigger>
                              <ContextMenuContent className="w-44">
                                <ContextMenuItem onSelect={() => openRenameChat(item.chat_id, label)}>
                                  <Pencil className="mr-2 size-4" />
                                  {t('nav.chatHistory.rename', 'Rename')}
                                </ContextMenuItem>
                                <ContextMenuSeparator />
                                <ContextMenuItem
                                  className="text-destructive focus:text-destructive"
                                  onSelect={() => { setDeleteChatFiles(false); setDeleteTarget({ chat_id: item.chat_id, label }); }}
                                >
                                  <Trash2 className="mr-2 size-4" />
                                  {t('nav.chatHistory.delete', 'Delete')}
                                </ContextMenuItem>
                              </ContextMenuContent>
                            </ContextMenu>
                          );
                        })}
                      </div>
                    ) : null}
                  </div>
                );
              })
            ) : (
              <div className="mx-1 rounded-lg border border-dashed border-edge-subtle px-3 py-4 text-center">
                <Folder className="mx-auto size-5 text-muted-foreground" />
                <p className="mt-2 text-[13px] font-medium text-foreground">
                  {t('nav.projects.emptyTitle', 'Create your first project')}
                </p>
                <p className="mt-1 text-xs leading-4 text-muted-foreground">
                  {t('nav.projects.emptyDescription', 'Chats in a project share files and a running workspace.')}
                </p>
                <Button
                  size="sm"
                  className="mt-3 h-8"
                  onClick={() => setCreateProjectOpen(true)}
                >
                  <FolderPlus className="mr-1.5 size-3.5" />
                  {t('nav.projects.new', 'New project')}
                </Button>
              </div>
            )}
          </div>
        </div>
      ) : <div className="min-h-0 flex-1" />}
      <div className="mt-auto shrink-0 border-t border-edge-subtle p-2">
        {!effectiveCollapsed && (
          <div className="flex min-w-0 items-center gap-2">
            <div className="min-w-0 flex-1 [&_[data-testid=organization-switcher]]:w-full [&_[data-testid=organization-switcher]]:max-w-none">
              <OrganizationSwitcher />
            </div>
            <div className="flex shrink-0 items-center">
              <UserMenuDropdown />
            </div>
          </div>
        )}
        {effectiveCollapsed && (
          <div className="flex flex-col items-center gap-1">
            <UserMenuDropdown />
          </div>
        )}
      </div>
      {!mobile ? <button
        type="button"
        onClick={toggle}
        data-testid="nav-sidebar-toggle"
        aria-label={
          effectiveCollapsed ? t('nav.expand', 'Expand sidebar') : t('nav.collapse', 'Collapse sidebar')
        }
        title={
          effectiveCollapsed ? t('nav.expand', 'Expand sidebar') : t('nav.collapse', 'Collapse sidebar')
        }
        className={cn(
          'flex items-center gap-2 border-t px-3 py-2 text-xs text-muted-foreground transition-colors hover:text-foreground',
          effectiveCollapsed && 'justify-center px-0',
        )}
      >
        {effectiveCollapsed ? (
          <ChevronRight className="h-4 w-4" />
        ) : (
          <>
            <ChevronLeft className="h-4 w-4" />
            <span>{t('nav.collapse', 'Collapse sidebar')}</span>
          </>
        )}
      </button> : null}
      </nav>
      <Dialog open={releaseProject !== null} onOpenChange={(open) => { if (!open) setReleaseProject(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('nav.projects.releaseTitle', 'Release project sandbox?')}</DialogTitle>
            <DialogDescription>{t('nav.projects.releaseDescription', 'This releases the shared runtime for all chats in this project. Saved files and conversation history remain available; the next message restores the workspace.')}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setReleaseProject(null)}>{t('cancel', 'Cancel')}</Button>
            <Button disabled={sandboxAction.isPending} onClick={() => releaseProject && void changeSandbox(releaseProject.project_id, 'release')}>
              {t('chat.sandbox.close_hint', 'Release sandbox')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog
        open={createProjectOpen}
        onOpenChange={(open) => {
          setCreateProjectOpen(open);
          if (!open) setCreateProjectName('');
        }}
      >
        <DialogContent>
          <form onSubmit={(event) => {
            event.preventDefault();
            void submitCreateProject();
          }}>
            <DialogHeader>
              <DialogTitle>{t('nav.projects.createTitle', 'Create a project')}</DialogTitle>
              <DialogDescription>
                {t(
                  'nav.projects.createDescription',
                  'Chats in the same project share files and a running workspace. You can add more chats at any time.',
                )}
              </DialogDescription>
            </DialogHeader>
            <Input
              className="mt-4"
              value={createProjectName}
              maxLength={120}
              autoFocus
              placeholder={t('nav.projects.namePlaceholder', 'e.g. Research assistant')}
              aria-label={t('nav.projects.name', 'Project name')}
              onChange={(event) => setCreateProjectName(event.target.value)}
            />
            <DialogFooter className="mt-5">
              <Button type="button" variant="outline" onClick={() => setCreateProjectOpen(false)}>
                {t('cancel', 'Cancel')}
              </Button>
              <Button type="submit" disabled={!createProjectName.trim() || createProject.isPending}>
                {createProject.isPending
                  ? t('common.creating', 'Creating…')
                  : t('nav.projects.createAction', 'Create project')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      <Dialog
        open={!!renameProjectTarget}
        onOpenChange={(open) => !open && setRenameProjectTarget(null)}
      >
        <DialogContent>
          <form onSubmit={(event) => {
            event.preventDefault();
            void submitRenameProject();
          }}>
            <DialogHeader>
              <DialogTitle>{t('nav.projects.renameTitle', 'Rename project')}</DialogTitle>
              <DialogDescription>
                {t('nav.projects.renameDescription', 'Use a name that describes the work shared by these chats.')}
              </DialogDescription>
            </DialogHeader>
            <Input
              className="mt-4"
              value={renameProjectDraft}
              maxLength={120}
              autoFocus
              aria-label={t('nav.projects.name', 'Project name')}
              onFocus={(event) => event.currentTarget.select()}
              onChange={(event) => setRenameProjectDraft(event.target.value)}
            />
            <DialogFooter className="mt-5">
              <Button type="button" variant="outline" onClick={() => setRenameProjectTarget(null)}>
                {t('cancel', 'Cancel')}
              </Button>
              <Button type="submit" disabled={!renameProjectDraft.trim() || renameProject.isPending}>
                {renameProject.isPending
                  ? t('common.saving', 'Saving…')
                  : t('nav.projects.rename', 'Rename project')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      <Dialog
        open={!!deleteProjectTarget}
        onOpenChange={(open) => !open && setDeleteProjectTarget(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('nav.projects.deleteTitle', 'Delete this project?')}</DialogTitle>
            <DialogDescription>
              {t(
                'nav.projects.deleteDescription',
                'This permanently deletes every chat in the project, its files, and its saved runtime state.',
              )}
            </DialogDescription>
          </DialogHeader>
          {deleteProjectTarget ? (
            <div className="rounded-md bg-muted/50 px-3 py-2 text-sm font-medium">
              {deleteProjectTarget.label}
            </div>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setDeleteProjectTarget(null)}>
              {t('cancel', 'Cancel')}
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={deleteProject.isPending}
              onClick={() => void submitDeleteProject()}
            >
              {deleteProject.isPending
                ? t('deleting', 'Deleting...')
                : t('nav.projects.delete', 'Delete project')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog
        open={!!renameTarget}
        onOpenChange={(open) => {
          if (!open) setRenameTarget(null);
        }}
      >
        <DialogContent>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void submitRenameChat();
            }}
          >
            <DialogHeader>
              <DialogTitle>{t('nav.chatHistory.renameTitle', 'Rename chat')}</DialogTitle>
              <DialogDescription>
                {t('nav.chatHistory.renameDescription', 'Choose a short name that makes this conversation easy to find.')}
              </DialogDescription>
            </DialogHeader>
            <Input
              className="mt-4"
              value={renameDraft}
              maxLength={120}
              autoFocus
              aria-label={t('nav.chatHistory.name', 'Chat name')}
              onFocus={(event) => event.currentTarget.select()}
              onChange={(event) => setRenameDraft(event.target.value)}
            />
            <DialogFooter className="mt-5">
              <Button type="button" variant="outline" onClick={() => setRenameTarget(null)}>
                {t('cancel', 'Cancel')}
              </Button>
              <Button
                type="submit"
                disabled={
                  renameChat.isPending
                  || !renameDraft.trim()
                  || renameDraft.trim().replace(/\s+/g, ' ') === renameTarget?.label
                }
              >
                {renameChat.isPending
                  ? t('common.saving', 'Saving…')
                  : t('nav.chatHistory.rename', 'Rename')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      <Dialog open={!!deleteTarget} onOpenChange={(open) => {
        if (!open) { setDeleteTarget(null); setDeleteChatFiles(false); }
      }}>
        <DialogContent className="min-w-0 overflow-x-hidden">
          <DialogHeader className="min-w-0">
            <DialogTitle>
              {t('nav.chatHistory.deleteTitle', 'Delete this chat?')}
            </DialogTitle>
            <DialogDescription>
              {t(
                'nav.chatHistory.deleteConfirm',
                'This removes only this conversation. Other chats and shared project files are kept.',
              )}
            </DialogDescription>
          </DialogHeader>
          {deleteTarget && (
            <div
              className="app-scrollbar min-w-0 max-w-full overflow-x-auto rounded-md bg-muted/50 px-3 py-2 text-sm"
              data-role="chat-delete-name-scroll"
              title={deleteTarget.label}
            >
              <span className="block w-max min-w-full whitespace-nowrap">
                {deleteTarget.label}
              </span>
            </div>
          )}
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={deleteChatFiles}
              onChange={(event) => setDeleteChatFiles(event.target.checked)}
              className="mt-1 accent-primary"
            />
            {t('nav.chatHistory.deleteFiles', 'Also permanently delete this chat’s working files. Shared project files are kept.')}
          </label>
          <DialogFooter className="min-w-0 shrink-0">
            <Button
              type="button"
              variant="outline"
              onClick={() => setDeleteTarget(null)}
            >
              {t('cancel', 'Cancel')}
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={deleteChat.isPending}
              onClick={() => void submitDeleteChat()}
            >
              {deleteChat.isPending
                ? t('deleting', 'Deleting...')
                : t('nav.chatHistory.delete', 'Delete')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
