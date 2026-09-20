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
import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { NavLink, useLocation, useNavigate } from 'react-router';
import {
  ChevronLeft,
  ChevronRight,
  Pencil,
  Trash2,
} from 'lucide-react';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
import { useUIStore } from '@/stores/ui';
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
  useDeleteChatSession,
  useRenameChatSession,
  fetchChatHistory,
  useChatSandboxStatuses,
  useChatSessions,
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
  chat_context: string;
  surface?: 'chat' | 'browser';
  __draft?: boolean;
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
      { to: '/chat', kind: 'chat', labelKey: 'nav.chat', fallback: 'Chat' },
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
  const setActiveChatId = useUIStore((s) => s.setActiveChatId);
  const setChatEntryIntent = useUIStore((s) => s.setChatEntryIntent);
  const draftChatSessions = useUIStore((s) => s.draftChatSessions);
  const optimisticChatSessions = useUIStore((s) => s.optimisticChatSessions);
  const removeOptimisticChatSession = useUIStore((s) => s.removeOptimisticChatSession);
  const [deleteTarget, setDeleteTarget] = useState<{
    chat_id: string;
    label: string;
  } | null>(null);
  const [renameTarget, setRenameTarget] = useState<{
    chat_id: string;
    label: string;
  } | null>(null);
  const [renameDraft, setRenameDraft] = useState('');
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
  const boot = useGeneralChatBootstrap();
  const carrierScopeId = boot.data?.carrier_scope_id ?? null;
  const sessions = useChatSessions(carrierScopeId);
  const deleteChat = useDeleteChatSession(carrierScopeId, 'chat');
  const renameChat = useRenameChatSession(carrierScopeId, 'chat');
  const effectiveCollapsed = mobile ? false : collapsed;
  const showChatContext = location.pathname === '/chat' && !effectiveCollapsed;
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
    const drafts: SidebarChatItem[] = draftChatSessions
      .filter(
        (item) =>
          item.scopeId === carrierScopeId &&
          item.surface === 'chat' &&
          !persistedIds.has(item.chat_id),
      )
      .map((item) => ({
        chat_id: item.chat_id,
        chat_context: t('new_chat', 'New Chat'),
        surface: item.surface,
        __draft: true,
      }));
    const optimistic: SidebarChatItem[] = optimisticForScope
      .filter((item) => !persistedIds.has(item.chat_id))
      .filter((item) => !drafts.some((draft) => draft.chat_id === item.chat_id))
      .map((item) => ({
        chat_id: item.chat_id,
        chat_context: item.chat_context,
        surface: item.surface,
      }));
    const optimisticIds = new Set(optimistic.map((item) => item.chat_id));
    const draftIds = new Set(drafts.map((item) => item.chat_id));
    const draft =
      activeChatId &&
      !persistedIds.has(activeChatId) &&
      !optimisticIds.has(activeChatId) &&
      !draftIds.has(activeChatId)
        ? [{
            chat_id: activeChatId,
            chat_context: t('new_chat', 'New Chat'),
            surface: 'chat' as const,
            __draft: true,
          }]
        : [];
    return [...drafts, ...draft, ...optimistic, ...persisted];
  }, [activeChatId, carrierScopeId, draftChatSessions, optimisticChatSessions, sessions.data?.items, t]);
  const sandboxStatuses = useChatSandboxStatuses(
    chatItems.filter((item) => !item.__draft).map((item) => item.chat_id),
  );
  const sandboxStatusByChat = useMemo(() => {
    const map = new Map<
      string,
      SandboxLifecycleStatus
    >();
    for (const item of sandboxStatuses.data?.items ?? []) {
      map.set(item.chat_id, item.status);
    }
    return map;
  }, [sandboxStatuses.data?.items]);

  const selectChat = (chatId: string) => {
    setChatEntryIntent('select');
    if (!carrierScopeId || chatId === activeChatId) {
      if (location.pathname !== '/chat') navigate('/chat');
      onNavigate?.();
      return;
    }
    const isDraft = draftChatSessions.some(
      (item) => item.scopeId === carrierScopeId && item.chat_id === chatId,
    );
    if (isDraft) {
      setActiveChatId('chat', chatId);
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

  const preloadChat = (chatId: string) => {
    if (!carrierScopeId) return;
    const isDraft = draftChatSessions.some(
      (item) => item.scopeId === carrierScopeId && item.chat_id === chatId,
    );
    if (isDraft) return;
    void queryClient.prefetchQuery({
      queryKey: ['chat-history', carrierScopeId, chatId, null],
      queryFn: () => fetchChatHistory(carrierScopeId, chatId),
      staleTime: 15_000,
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
                        if (item.to === '/chat') setChatEntryIntent('default');
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
          <div className="px-3 text-xs font-semibold text-muted-foreground">
            {t('nav.chatHistory', 'Chat history')}
          </div>
          <div className="app-scrollbar mt-2 min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 pb-2">
            {boot.isLoading || sessions.isLoading ? (
              <div className="space-y-1 px-3">
                <Skeleton className="h-7 w-full" />
                <Skeleton className="h-7 w-5/6" />
              </div>
            ) : chatItems.length ? (
              chatItems.map((item) => {
                const label = item.chat_context || item.chat_id.slice(0, 8);
                const status = sandboxStatusByChat.get(item.chat_id) ?? 'idle';
                const running = status === 'running';
                const button = (
                  <button
                    type="button"
                    title={label}
                    data-chat-id={item.chat_id}
                    onPointerEnter={() => preloadChat(item.chat_id)}
                    onFocus={() => preloadChat(item.chat_id)}
                    onClick={() => selectChat(item.chat_id)}
                    className={cn(
                      'flex h-9 w-full items-center gap-2 rounded-md px-3 text-left text-ui font-medium transition-colors duration-150',
                      !item.__draft && 'pr-9',
                      item.chat_id === activeChatId
                        ? 'bg-primary/[0.07] font-semibold text-foreground'
                        : 'text-muted-foreground hover:bg-surface-hover/70 hover:text-foreground',
                    )}
                  >
                    <span className="relative shrink-0">
                      <ResourceIcon kind="chat" size="sm" className="size-6 rounded-md" />
                      {running ? (
                        <StatusDot
                          status="success"
                          className="absolute -bottom-0.5 -right-0.5 ring-2 ring-surface-nav"
                          title={t('chat.sandbox.running', 'Sandbox running')}
                          aria-label={t('chat.sandbox.running', 'Sandbox running')}
                        />
                      ) : (
                        <span className="sr-only">{t('chat.sandbox.idle', 'Sandbox idle')}</span>
                      )}
                    </span>
                    <span className="block min-w-0 flex-1 truncate text-left">
                      {label}
                    </span>
                  </button>
                );
                const row = (
                  <div className="group/chat-history relative flex items-center">
                    <div className="min-w-0 flex-1">{button}</div>
                    {!item.__draft && (
                      <button
                        type="button"
                        className="absolute right-1 grid h-7 w-7 place-items-center rounded-md text-muted-foreground opacity-0 transition-opacity hover:bg-surface-raised hover:text-destructive focus-visible:opacity-100 group-hover/chat-history:opacity-100"
                        aria-label={t('nav.chatHistory.delete', 'Delete')}
                        title={t('nav.chatHistory.delete', 'Delete')}
                        onClick={(event) => {
                          event.stopPropagation();
                          setDeleteTarget({ chat_id: item.chat_id, label });
                        }}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </div>
                );
                if (item.__draft) return <div key={item.chat_id}>{row}</div>;
                return (
                  <ContextMenu key={item.chat_id}>
                    <ContextMenuTrigger asChild>{row}</ContextMenuTrigger>
                    <ContextMenuContent className="w-44">
                      <ContextMenuItem onSelect={() => openRenameChat(item.chat_id, label)}>
                        <Pencil className="mr-2 h-4 w-4" />
                        {t('nav.chatHistory.rename', 'Rename')}
                      </ContextMenuItem>
                      <ContextMenuSeparator />
                      <ContextMenuItem
                        className="text-destructive focus:text-destructive"
                        onSelect={() => setDeleteTarget({ chat_id: item.chat_id, label })}
                      >
                        <Trash2 className="mr-2 h-4 w-4" />
                        {t('nav.chatHistory.delete', 'Delete')}
                      </ContextMenuItem>
                    </ContextMenuContent>
                  </ContextMenu>
                );
              })
            ) : (
              <div className="px-3 py-1.5 text-[13px] text-muted-foreground">
                {t('nav.chatHistory.empty', 'No chats yet.')}
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
      <Dialog open={!!deleteTarget} onOpenChange={(open) => !open && setDeleteTarget(null)}>
        <DialogContent className="min-w-0 overflow-x-hidden">
          <DialogHeader className="min-w-0">
            <DialogTitle>
              {t('nav.chatHistory.deleteTitle', 'Delete this chat?')}
            </DialogTitle>
            <DialogDescription>
              {t(
                'nav.chatHistory.deleteConfirm',
                'This will delete the conversation history and its workspace files under data, memory, and logs. This cannot be undone.',
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
