import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router';
import { toast } from 'sonner';
import {
  ArrowLeft,
  Link2,
  Loader2,
  PlugZap,
  RefreshCw,
  Save,
  Trash2,
  Unplug,
} from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Textarea } from '@/components/ui/textarea';
import { StatusBadge, type SemanticStatus } from '@/components/ui/status';
import { CopyButton } from '@/components/ui/copy-button';
import { EntityDetailShell } from '@/components/layout/entity-detail-shell';
import { DetailSummary } from '@/components/layout/detail-summary';
import { SectionBlock } from '@/components/layout/section-block';
import { useFormatDateTime } from '@/lib/timezone';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  useDeleteMcpServer,
  useDisconnectMcpOAuth,
  useMcpServer,
  useRefreshMcpServer,
  useStartMcpOAuth,
  useUpdateMcpServer,
} from '@/lib/api/queries/mcp-servers';
import { McpToolDirectory } from './McpToolDirectory';
import { ActionableError } from '@/components/presentation/ActionableError';
import { CompactEmptyState } from '@/components/presentation/CompactEmptyState';
import { ResourceProvenanceLine } from '@/components/resources/ResourceProvenanceLine';

function formatSource(source?: string | null): string {
  if (!source) return 'Fallback';
  return source
    .split('_')
    .map((p) => p.charAt(0).toUpperCase() + p.slice(1))
    .join(' ');
}

function statusPill(server: {
  enabled: boolean;
  last_handshake_status: string | null;
  connection_status: string;
}): { key: string; fallback: string; status: SemanticStatus } {
  if (server.connection_status === 'connection_required') {
    return {
      key: 'mcp.status.connection_required',
      fallback: 'Connection Required',
      status: 'warning',
    };
  }
  if (server.connection_status === 'connecting') {
    return {
      key: 'mcp.status.connecting',
      fallback: 'Connecting',
      status: 'running',
    };
  }
  if (server.connection_status === 'reconnect_required') {
    return {
      key: 'mcp.status.reconnect_required',
      fallback: 'Reconnect Required',
      status: 'warning',
    };
  }
  if (server.connection_status === 'connection_failed') {
    return {
      key: 'mcp.status.connection_failed',
      fallback: 'Connection Failed',
      status: 'danger',
    };
  }
  if (!server.enabled) {
    return {
      key: 'mcp.status.disabled',
      fallback: 'Disabled',
      status: 'neutral',
    };
  }
  if (server.last_handshake_status === 'ok') {
    return {
      key: 'mcp.status.active',
      fallback: 'Active',
      status: 'success',
    };
  }
  if ((server.last_handshake_status ?? '').startsWith('error')) {
    return {
      key: 'mcp.status.probe_failed',
      fallback: 'Probe Failed',
      status: 'danger',
    };
  }
  return {
    key: 'mcp.status.needs_probe',
    fallback: 'Needs Probe',
    status: 'warning',
  };
}

export function McpServerDetailPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const formatTime = useFormatDateTime();
  const [searchParams, setSearchParams] = useSearchParams();
  const { id } = useParams();
  const query = useMcpServer(id);
  const updateMutation = useUpdateMcpServer();
  const refreshMutation = useRefreshMcpServer();
  const oauthStartMutation = useStartMcpOAuth();
  const oauthDisconnectMutation = useDisconnectMcpOAuth();
  const deleteMutation = useDeleteMcpServer();
  const [descriptionDraft, setDescriptionDraft] = useState('');
  const [confirmation, setConfirmation] = useState<'disconnect' | 'uninstall' | null>(null);
  const oauthCallbackOrigin = useRef<string | null>(null);

  const backLink = (
    <Link
      to="/mcp-servers"
      className="inline-flex items-center gap-1 text-sm text-primary underline-offset-4 hover:underline"
    >
      <ArrowLeft className="h-4 w-4" />
      {t('mcp.back', 'Back')}
    </Link>
  );

  const server = query.data;

  useEffect(() => {
    if (server) queueMicrotask(() => setDescriptionDraft(server.description ?? ''));
  }, [server]);

  useEffect(() => {
    if (!server || server.auth_mode !== 'oauth') return;
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin && event.origin !== oauthCallbackOrigin.current) return;
      const payload = event.data as {
        type?: string;
        serverId?: string;
        ok?: boolean;
        message?: string;
      };
      if (payload.type !== 'vibecanvas:mcp-oauth-complete' || payload.serverId !== server.id) return;
      void query.refetch();
      if (payload.ok) toast.success(t('mcp.oauth.connected', 'Account Connected'));
      else toast.error(payload.message || t('mcp.oauth.failed', 'Account Connection Failed'));
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [query, server, t]);

  useEffect(() => {
    if (server?.connection_status !== 'connecting') return;
    const timer = window.setInterval(() => void query.refetch(), 2000);
    return () => window.clearInterval(timer);
  }, [query, server?.connection_status]);

  const defaultTab = useMemo(() => {
    if (!server) return 'overview';
    if (!server.enabled || (server.last_handshake_status ?? '').startsWith('error')) {
      return 'connection';
    }
    return 'overview';
  }, [server]);
  const requestedTab = searchParams.get('tab');
  const activeTab = requestedTab === 'tools' ? 'tools' : ['connection', 'security', 'config'].includes(requestedTab ?? '') ? 'connection' : ['overview','basic','brief'].includes(requestedTab ?? '') ? 'overview' : defaultTab;

  if (query.isLoading) {
    return (
      <div className="page-shell page-shell-contained">
        <div className="mx-auto flex min-w-0 max-w-5xl flex-col gap-6">
          {backLink}
          <div className="rounded-md border p-10 text-center text-sm text-muted-foreground">
            {t('mcp.loading', 'Loading…')}
          </div>
        </div>
      </div>
    );
  }

  if (query.isError) {
    return (
      <div className="page-shell page-shell-contained">
        <div className="mx-auto flex min-w-0 max-w-5xl flex-col gap-6">
          {backLink}
          <ActionableError
            title={t('mcp.load_error', 'Failed to load MCP servers.')}
            description={t('mcp.load_error_hint', 'Check the connection, then load this server again.')}
            actionLabel={t('retry', 'Retry')}
            onAction={() => void query.refetch()}
            technicalDetails={query.error instanceof Error ? query.error.message : String(query.error ?? '')}
            technicalDetailsLabel={t('common.technicalDetails', 'Technical details')}
          />
        </div>
      </div>
    );
  }

  if (!server) {
    return (
      <div className="page-shell page-shell-contained">
        <div className="mx-auto flex min-w-0 max-w-5xl flex-col gap-6">
          {backLink}
          <CompactEmptyState title={t('mcp.not_found', 'This MCP server no longer exists.')} />
        </div>
      </div>
    );
  }

  const status = statusPill(server);
  const statusText = t(status.key, status.fallback);
  const tools = server.last_tool_names ?? [];
  const descriptionDirty = descriptionDraft !== (server.description ?? '');
  const capabilities = new Set(server.access?.capabilities ?? []);
  const configJson = JSON.stringify(
    {
      name: server.name,
      tool_prefix: server.tool_prefix,
      transport: server.transport,
      endpoint: server.endpoint,
      connection_config: server.connection_config ?? {},
      enabled: server.enabled,
      auth_config: server.auth_config,
      description_source: server.description_source,
    },
    null,
    2,
  );

  const handleSaveDescription = async () => {
    try {
      await updateMutation.mutateAsync({
        id: server.id,
        patch: {
          description: descriptionDraft,
          description_source: 'user_edited',
        },
      });
      toast.success(t('mcp.detail.brief.saved', 'Brief description saved'));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const handleRefresh = async () => {
    try {
      await refreshMutation.mutateAsync(server.id);
      toast.success(t('mcp.refreshed', 'Connection test refreshed'));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const handleToggle = async () => {
    try {
      await updateMutation.mutateAsync({
        id: server.id,
        patch: { enabled: !server.enabled },
      });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const handleOAuthConnect = async () => {
    const popup = window.open('', 'vibecanvas-mcp-oauth', 'popup,width=560,height=720');
    if (!popup) {
      toast.error(t('mcp.oauth.popup_blocked', 'Allow pop-ups to connect this account.'));
      return;
    }
    popup.document.title = t('mcp.oauth.connecting', 'Connecting Account');
    try {
      const { authorization_url, callback_origin } = await oauthStartMutation.mutateAsync(server.id);
      oauthCallbackOrigin.current = callback_origin;
      popup.location.replace(authorization_url);
    } catch (e) {
      popup.close();
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const handleOAuthDisconnect = async () => {
    try {
      await oauthDisconnectMutation.mutateAsync(server.id);
      setConfirmation(null);
      toast.success(t('mcp.oauth.disconnected', 'Account Disconnected'));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const handleDelete = async () => {
    try {
      await deleteMutation.mutateAsync(server.id);
      toast.success(t('mcp.deleted', 'MCP server uninstalled'));
      navigate('/mcp-servers');
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <EntityDetailShell
      resourceKind="mcp"
      backTo="/mcp-servers"
      backLabel={t('mcp.back', 'Back')}
      title={server.name}
      icon={PlugZap}
      status={<StatusBadge status={status.status}>{statusText}</StatusBadge>}
      metadata={<><span className="font-mono">{server.tool_prefix}</span><span>{server.transport}</span><span>{formatSource(server.description_source)}</span><ResourceProvenanceLine provenance={server.provenance} /></>}
      actions={<>
              {capabilities.has('update') ? <Button
                variant="outline"
                size="sm"
                onClick={() => void handleRefresh()}
                disabled={refreshMutation.isPending || (server.auth_mode === 'oauth' && server.connection_status !== 'connected')}
              >
                <RefreshCw className={refreshMutation.isPending ? 'animate-spin' : ''} />
                {t('mcp.refresh', 'Test connection')}
              </Button> : null}
              {capabilities.has('update') ? <Button
                variant="outline"
                size="sm"
                onClick={() => void handleToggle()}
                disabled={updateMutation.isPending || (server.auth_mode === 'oauth' && server.connection_status !== 'connected')}
              >
                {server.enabled ? t('mcp.disable', 'Disable') : t('mcp.enable', 'Enable')}
              </Button> : null}
              {capabilities.has('delete') ? <Button
                variant="destructive"
                size="sm"
                onClick={() => setConfirmation('uninstall')}
                disabled={deleteMutation.isPending}
              >
                <Trash2 />
                {t('mcp.delete', 'Uninstall')}
              </Button> : null}
            </>}
    >

        <Tabs
          key={server.id}
          value={activeTab}
          onValueChange={(tab) => {
            const next = new URLSearchParams(searchParams);
            next.set('tab', tab);
            setSearchParams(next, { replace: true });
          }}
          className="flex min-h-0 flex-1 flex-col overflow-hidden border-y border-edge-subtle bg-surface-work"
        >
          <TabsList variant="underline" className="chat-scrollbar h-auto w-full justify-start overflow-x-auto px-4">
            {[
              ['overview', t('skills.detail.tab.overview', 'Overview')],
              ['tools', `${t('mcp.detail.tab.tools', 'Tools')} ${tools.length}`],
              ['connection', t('mcp.detail.connectionSettings', 'Connection & settings')],
            ].map(([value, label]) => (
              <TabsTrigger
                key={value}
                value={value}
                className="shrink-0 px-1 py-3"
              >
                {label}
                {value === 'connection' && status.key === 'mcp.status.probe_failed' ? (
                  <span className="ml-2 h-1.5 w-1.5 rounded-full bg-destructive" />
                ) : null}
              </TabsTrigger>
            ))}
          </TabsList>

          <TabsContent value="overview" className="page-scroll-region mt-0 min-h-0 flex-1 p-5 data-[state=inactive]:hidden">
            <div className="flex flex-col gap-4">
              <div>
                <div className="mb-1 flex items-center justify-between gap-3">
                  <div>
                    <h2 className="text-sm font-medium">{t('mcp.detail.brief.title', 'Brief description')}</h2>
                    <p className="text-xs text-muted-foreground">
                      {t('mcp.detail.brief.help', 'Agents use this short description to decide when this MCP server may be useful.')}
                    </p>
                  </div>
                  <span className="rounded-full bg-secondary px-2 py-0.5 text-xs text-secondary-foreground">
                    {formatSource(server.description_source)}
                  </span>
                </div>
                <Textarea
                  aria-label={t('mcp.detail.brief.title', 'Brief description')}
                  value={descriptionDraft}
                  onChange={(e) => setDescriptionDraft(e.target.value)}
                  rows={6}
                  maxLength={2000}
                  className="resize-y"
                  readOnly={!capabilities.has('update')}
                />
              </div>
              <div className="flex justify-end">
                  {capabilities.has('update') ? <Button
                    onClick={() => void handleSaveDescription()}
                    disabled={!descriptionDirty || updateMutation.isPending}
                  >
                    <Save />
                    {t('mcp.save', 'Save')}
                  </Button> : null}
              </div>
            </div>
            <SectionBlock variant="plain" collapsible defaultOpen={false} title={t('mcp.detail.summary', 'Server summary')}>
              <DetailSummary items={[
                { label: t('mcp.detail.id', 'MCP ID'), value: <span className="flex min-w-0 items-center gap-2"><code className="min-w-0 break-all text-xs">{server.id}</code><CopyButton value={server.id} className="shrink-0" /></span>, wide: true },
                { label: t('mcp.detail.created', 'Created'), value: formatTime(server.created_at) },
                { label: t('mcp.detail.updated', 'Updated'), value: formatTime(server.updated_at) },
              ]} />
            </SectionBlock>
          </TabsContent>

          <TabsContent value="connection" className="page-scroll-region mt-0 min-h-0 flex-1 p-5 data-[state=inactive]:hidden">
            <SectionBlock variant="plain" title={t('mcp.detail.configSummary', 'Effective configuration')}>
              <DetailSummary items={[
                {label:t('mcp.endpoint','Endpoint'),value:<span className="flex items-center gap-2"><code className="min-w-0 break-all text-xs">{server.endpoint}</code><CopyButton value={server.endpoint} /></span>,wide:true},
                {label:t('mcp.tool_prefix','Tool prefix'),value:<code>{server.tool_prefix}</code>},
              ]} />
            </SectionBlock>
            {server.auth_mode === 'oauth' ? (
              <div className="space-y-5">
                <div className="flex flex-col gap-4 rounded-lg border bg-muted/20 p-4 sm:flex-row sm:items-center sm:justify-between">
                  <div className="flex items-start gap-3">
                    <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-background text-muted-foreground shadow-sm">
                      {server.connection_status === 'connecting'
                        ? <Loader2 className="h-4 w-4 animate-spin" />
                        : <Link2 className="h-4 w-4" />}
                    </div>
                    <div>
                      <h2 className="text-sm font-medium">{t('mcp.oauth.account_connection', 'Account Connection')}</h2>
                      <p className="mt-1 max-w-xl text-sm text-muted-foreground">
                        {server.connection_status === 'connected'
                          ? t('mcp.oauth.connected_help', 'The account is connected and this server can be loaded by agents.')
                          : t('mcp.oauth.required_help', 'Connect the external account required by this MCP server. Installing alone does not grant access.')}
                      </p>
                    </div>
                  </div>
                  {capabilities.has('manage_secret') && server.connection_status === 'connected' ? (
                    <Button
                      variant="outline"
                      onClick={() => setConfirmation('disconnect')}
                      disabled={oauthDisconnectMutation.isPending}
                    >
                      <Unplug />
                      {t('mcp.oauth.disconnect', 'Disconnect')}
                    </Button>
                  ) : capabilities.has('manage_secret') ? (
                    <Button
                      onClick={() => void handleOAuthConnect()}
                      disabled={oauthStartMutation.isPending || server.connection_status === 'connecting'}
                    >
                      {oauthStartMutation.isPending || server.connection_status === 'connecting'
                        ? <Loader2 className="animate-spin" />
                        : <Link2 />}
                      {server.connection_status === 'reconnect_required'
                        ? t('mcp.oauth.reconnect', 'Reconnect Account')
                        : t('mcp.oauth.connect', 'Connect Account')}
                    </Button>
                  ) : null}
                </div>
                <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                  <Info label={t('mcp.detail.connection_status', 'Connection status')} value={statusText} />
                  <Info label={t('mcp.detail.last_probe', 'Last probe')} value={server.last_handshake_at ?? t('mcp.detail.never', 'Never')} />
                  <div className="md:col-span-2">
                    <Info label={t('mcp.detail.probe_status', 'Probe status')} value={server.last_handshake_status ?? t('mcp.detail.never_probed', 'Never probed')} />
                  </div>
                </div>
              </div>
            ) : (
              <SectionBlock title={t('mcp.detail.connectionHealth', 'Connection health')}>
                <DetailSummary items={[
                  { label: t('mcp.detail.auth_type', 'Auth type'), value: server.auth_config?.type ?? 'none' },
                  {
                    label: t('mcp.detail.credential_status', 'Credential status'),
                    value: server.auth_config?.token ? t('mcp.detail.token_saved', 'Token saved') : t('mcp.detail.no_credential', 'No credential required'),
                  },
                  { label: t('mcp.detail.last_probe', 'Last probe'), value: server.last_handshake_at ? formatTime(server.last_handshake_at) : t('mcp.detail.never', 'Never') },
                  { label: t('mcp.detail.probe_status', 'Probe status'), value: server.last_handshake_status ?? t('mcp.detail.never_probed', 'Never probed') },
                ]} />
              </SectionBlock>
            )}
            <SectionBlock variant="plain" collapsible defaultOpen={false}
              title={t('mcp.detail.rawConfig','Raw JSON')} actions={<CopyButton value={configJson} />}>
              <pre className="max-h-96 overflow-auto rounded border p-3 text-xs">{configJson}</pre>
              <p className="mt-3 text-xs text-muted-foreground">{t('mcp.detail.secretEncrypted','Encrypted and never returned in plaintext')}</p>
            </SectionBlock>
          </TabsContent>

          <TabsContent value="tools" className="mt-0 flex min-h-0 flex-1 flex-col overflow-hidden p-0 data-[state=inactive]:hidden">
            {tools.length === 0 ? (
              <div className="m-5 flex flex-col items-center rounded-lg border border-dashed border-edge-subtle p-8 text-center">
                <p className="text-sm text-muted-foreground">{t('mcp.no_tools', 'No tools probed yet.')}</p>
                {capabilities.has('update') ? (
                  <Button
                    variant="outline"
                    size="sm"
                    className="mt-4"
                    onClick={() => void handleRefresh()}
                    disabled={refreshMutation.isPending}
                  >
                    <RefreshCw className={refreshMutation.isPending ? 'animate-spin' : ''} />
                    {t('mcp.detail.tools.discover', 'Discover tools')}
                  </Button>
                ) : null}
              </div>
            ) : (
              <McpToolDirectory tools={tools} />
            )}
          </TabsContent>

        </Tabs>
        <Dialog open={confirmation !== null} onOpenChange={(open) => !open && setConfirmation(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>
                {confirmation === 'disconnect'
                  ? t('mcp.oauth.disconnect', 'Disconnect account')
                  : t('mcp.delete', 'Uninstall MCP server')}
              </DialogTitle>
              <DialogDescription>
                {confirmation === 'disconnect'
                  ? t('mcp.oauth.disconnect_confirm', 'Disconnect this external account? The MCP server will be disabled.')
                  : t('mcp.uninstall_confirm', 'Uninstall this MCP server? Agents will no longer be able to load its tools.')}
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="outline" onClick={() => setConfirmation(null)}>
                {t('cancel', 'Cancel')}
              </Button>
              <Button
                variant="danger"
                disabled={deleteMutation.isPending || oauthDisconnectMutation.isPending}
                onClick={() => {
                  if (confirmation === 'disconnect') void handleOAuthDisconnect();
                  if (confirmation === 'uninstall') void handleDelete();
                }}
              >
                {confirmation === 'disconnect'
                  ? t('mcp.oauth.disconnect', 'Disconnect')
                  : t('mcp.delete', 'Uninstall')}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
    </EntityDetailShell>
  );
}

function Info({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string | number | null | undefined;
  mono?: boolean;
}) {
  return (
    <div>
      <div className="mb-1 text-xs text-muted-foreground">{label}</div>
      <div className={mono ? 'font-mono text-sm' : 'text-sm'}>
        {value || '—'}
      </div>
    </div>
  );
}
