import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Copy, KeyRound, Plus, RotateCw, ShieldCheck } from 'lucide-react';
import { useTranslation } from 'react-i18next';
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
  createEnterpriseIdentityProvider,
  listEnterpriseIdentityProviders,
  rotateEnterpriseScimToken,
  setEnterpriseIdentityProviderStatus,
  updateEnterpriseOidcClientAuth,
  type EnterpriseIdentityProvider,
} from '@/lib/api/enterprise-identity';

const identityProvidersKey = (organizationId: string) =>
  ['enterprise-identity-providers', organizationId] as const;

function message(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

interface EnterpriseIdentityPanelProps {
  organizationId: string;
}

export function EnterpriseIdentityPanel({
  organizationId,
}: EnterpriseIdentityPanelProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const providers = useQuery({
    queryKey: identityProvidersKey(organizationId),
    queryFn: () => listEnterpriseIdentityProviders(organizationId),
    enabled: Boolean(organizationId),
  });
  const [createOpen, setCreateOpen] = useState(false);
  const [displayName, setDisplayName] = useState('');
  const [issuerUrl, setIssuerUrl] = useState('');
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [tokenAuthMethod, setTokenAuthMethod] = useState<
    'client_secret_basic' | 'client_secret_post' | 'none'
  >('client_secret_basic');
  const [issuedToken, setIssuedToken] = useState<{
    providerName: string;
    value: string;
  } | null>(null);
  const [editingProvider, setEditingProvider] = useState<EnterpriseIdentityProvider | null>(null);
  const [editTokenAuthMethod, setEditTokenAuthMethod] = useState<
    'client_secret_basic' | 'client_secret_post' | 'none'
  >('client_secret_basic');
  const [replacementClientSecret, setReplacementClientSecret] = useState('');

  const revealToken = (provider: EnterpriseIdentityProvider) => {
    if (!provider.scim_token) return;
    setIssuedToken({ providerName: provider.display_name, value: provider.scim_token });
  };
  const refresh = () => queryClient.invalidateQueries({
    queryKey: identityProvidersKey(organizationId),
  });
  const createMutation = useMutation({
    mutationFn: () => createEnterpriseIdentityProvider(organizationId, {
      display_name: displayName.trim(),
      issuer_url: issuerUrl.trim(),
      client_id: clientId.trim(),
      token_endpoint_auth_method: tokenAuthMethod,
      ...(clientSecret ? { client_secret: clientSecret } : {}),
      scim_token_ttl_days: 365,
    }),
    onSuccess: async (provider) => {
      await refresh();
      setCreateOpen(false);
      setDisplayName('');
      setIssuerUrl('');
      setClientId('');
      setClientSecret('');
      setTokenAuthMethod('client_secret_basic');
      revealToken(provider);
      toast.success(t('organization.identity.created', 'Identity provider connected'));
    },
    onError: (reason) => toast.error(message(reason)),
  });
  const statusMutation = useMutation({
    mutationFn: ({ providerId, status }: {
      providerId: string;
      status: 'active' | 'disabled';
    }) => setEnterpriseIdentityProviderStatus(
      organizationId,
      providerId,
      status,
    ),
    onSuccess: async () => {
      await refresh();
      toast.success(t('organization.identity.updated', 'Identity provider updated'));
    },
    onError: (reason) => toast.error(message(reason)),
  });
  const rotateMutation = useMutation({
    mutationFn: (providerId: string) => rotateEnterpriseScimToken(
      organizationId,
      providerId,
      365,
    ),
    onSuccess: async (provider) => {
      await refresh();
      revealToken(provider);
    },
    onError: (reason) => toast.error(message(reason)),
  });
  const oidcClientMutation = useMutation({
    mutationFn: () => updateEnterpriseOidcClientAuth(
      organizationId,
      editingProvider!.provider_id,
      editTokenAuthMethod,
      replacementClientSecret || undefined,
    ),
    onSuccess: async () => {
      await refresh();
      setEditingProvider(null);
      setReplacementClientSecret('');
      toast.success(t('organization.identity.updated', 'Identity provider updated'));
    },
    onError: (reason) => toast.error(message(reason)),
  });

  return (
    <section data-testid="enterprise-identity-section">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="flex items-center gap-2 text-sm font-medium">
            <ShieldCheck className="size-4 text-primary" />
            {t('organization.identity.title', 'Enterprise identity')}
          </h3>
          <p className="max-w-[70ch] text-sm text-muted-foreground">
            {t(
              'organization.identity.description',
              'Use one OIDC connection for sign-in and its SCIM token for user and group lifecycle. Directory-managed groups remain read-only here.',
            )}
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => setCreateOpen(true)}>
          <Plus />
          {t('organization.identity.add', 'Add identity provider')}
        </Button>
      </div>

      {providers.isError ? (
        <p className="rounded-lg border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
          {t('organization.identity.loadFailed', 'Could not load identity providers.')}
        </p>
      ) : providers.data?.length ? (
        <div className="divide-y rounded-lg border border-edge-subtle">
          {providers.data.map((provider) => (
            <div key={provider.provider_id} className="grid gap-3 px-4 py-4 lg:grid-cols-[minmax(0,1fr)_auto]">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <p className="truncate text-sm font-medium">{provider.display_name}</p>
                  <span className={`rounded-full px-2 py-0.5 text-xs ${provider.status === 'active' ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300' : 'bg-muted text-muted-foreground'}`}>
                    {provider.status}
                  </span>
                  <span className="rounded-full bg-primary/10 px-2 py-0.5 text-xs text-primary">
                    OIDC + SCIM
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {provider.token_endpoint_auth_method}
                  </span>
                </div>
                <p className="mt-1 truncate text-xs text-muted-foreground" title={provider.issuer_url}>
                  {provider.issuer_url}
                </p>
                <div className="mt-2 grid gap-1 text-xs text-muted-foreground">
                  <p className="break-all">{t('organization.identity.scimEndpoint', 'SCIM endpoint')}: {provider.scim_base_url ?? '—'}</p>
                  <p className="break-all">{t('organization.identity.callbackUrl', 'Callback URL')}: {provider.oidc_callback_url ?? '—'}</p>
                  <p>
                    {t('organization.identity.tokenGeneration', 'SCIM token generation')}: {provider.scim_token_generation}
                    {' · '}
                    {t('organization.identity.lastSync', 'Last sync')}: {provider.last_scim_sync_at ?? '—'}
                  </p>
                </div>
              </div>
              <div className="flex flex-wrap items-start gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    setEditingProvider(provider);
                    setEditTokenAuthMethod(provider.token_endpoint_auth_method);
                    setReplacementClientSecret('');
                  }}
                >
                  <KeyRound />
                  {t('organization.identity.clientAuth', 'OIDC client auth')}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={rotateMutation.isPending}
                  onClick={() => rotateMutation.mutate(provider.provider_id)}
                >
                  <RotateCw />
                  {t('organization.identity.rotateToken', 'Rotate SCIM token')}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={statusMutation.isPending}
                  onClick={() => statusMutation.mutate({
                    providerId: provider.provider_id,
                    status: provider.status === 'active' ? 'disabled' : 'active',
                  })}
                >
                  {provider.status === 'active'
                    ? t('organization.disable', 'Disable')
                    : t('organization.enable', 'Enable')}
                </Button>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <p className="rounded-lg border border-edge-subtle p-4 text-sm text-muted-foreground">
          {t('organization.identity.empty', 'No enterprise identity provider is configured.')}
        </p>
      )}

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('organization.identity.add', 'Add identity provider')}</DialogTitle>
            <DialogDescription>
              {t('organization.identity.addDescription', 'The issuer discovery document is verified over public HTTPS before the configuration is stored.')}
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-2">
            <div className="grid gap-2">
              <Label htmlFor="identity-display-name">{t('organization.name', 'Name')}</Label>
              <Input id="identity-display-name" value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder={t('organization.identity.namePlaceholder', 'Corporate identity')} />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="identity-issuer">{t('organization.identity.issuer', 'OIDC issuer URL')}</Label>
              <Input id="identity-issuer" type="url" value={issuerUrl} onChange={(event) => setIssuerUrl(event.target.value)} placeholder="https://login.example.com" />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="identity-client-id">{t('organization.identity.clientId', 'Client ID')}</Label>
              <Input id="identity-client-id" value={clientId} onChange={(event) => setClientId(event.target.value)} autoComplete="off" />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="identity-token-auth-method">
                {t('organization.identity.tokenAuthMethod', 'Token endpoint authentication')}
              </Label>
              <select
                id="identity-token-auth-method"
                className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/25"
                value={tokenAuthMethod}
                onChange={(event) => {
                  const method = event.target.value as typeof tokenAuthMethod;
                  setTokenAuthMethod(method);
                  if (method === 'none') setClientSecret('');
                }}
              >
                <option value="client_secret_basic">client_secret_basic</option>
                <option value="client_secret_post">client_secret_post</option>
                <option value="none">none</option>
              </select>
              <p className="text-xs text-muted-foreground">
                {t('organization.identity.tokenAuthMethodDescription', 'Use the method advertised by the provider discovery document. Basic is the standard default; none is only for public clients.')}
              </p>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="identity-client-secret">
                {tokenAuthMethod === 'none'
                  ? t('organization.identity.clientSecretNotUsed', 'Client secret (not used)')
                  : t('organization.identity.clientSecret', 'Client secret')}
              </Label>
              <Input
                id="identity-client-secret"
                type="password"
                value={clientSecret}
                disabled={tokenAuthMethod === 'none'}
                onChange={(event) => setClientSecret(event.target.value)}
                autoComplete="new-password"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateOpen(false)}>{t('common_cancel', 'Cancel')}</Button>
            <Button
              disabled={!displayName.trim()
                || !issuerUrl.trim()
                || !clientId.trim()
                || (tokenAuthMethod !== 'none' && !clientSecret)
                || createMutation.isPending}
              onClick={() => createMutation.mutate()}
            >
              {t('common_create', 'Create')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={editingProvider !== null}
        onOpenChange={(open) => {
          if (!open) {
            setEditingProvider(null);
            setReplacementClientSecret('');
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('organization.identity.clientAuth', 'OIDC client auth')}</DialogTitle>
            <DialogDescription>
              {t('organization.identity.clientAuthDescription', 'Change the standard token endpoint authentication method or rotate the client secret. Existing secrets are never displayed.')}
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-2">
            <div className="grid gap-2">
              <Label htmlFor="identity-edit-token-auth-method">
                {t('organization.identity.tokenAuthMethod', 'Token endpoint authentication')}
              </Label>
              <select
                id="identity-edit-token-auth-method"
                className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/25"
                value={editTokenAuthMethod}
                onChange={(event) => {
                  const method = event.target.value as typeof editTokenAuthMethod;
                  setEditTokenAuthMethod(method);
                  if (method === 'none') setReplacementClientSecret('');
                }}
              >
                <option value="client_secret_basic">client_secret_basic</option>
                <option value="client_secret_post">client_secret_post</option>
                <option value="none">none</option>
              </select>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="identity-replacement-client-secret">
                {editTokenAuthMethod === 'none'
                  ? t('organization.identity.clientSecretNotUsed', 'Client secret (not used)')
                  : t('organization.identity.replacementClientSecret', 'Replacement client secret')}
              </Label>
              <Input
                id="identity-replacement-client-secret"
                type="password"
                value={replacementClientSecret}
                disabled={editTokenAuthMethod === 'none'}
                onChange={(event) => setReplacementClientSecret(event.target.value)}
                autoComplete="new-password"
              />
              {editTokenAuthMethod !== 'none' ? (
                <p className="text-xs text-muted-foreground">
                  {t('organization.identity.keepClientSecret', 'Leave blank to keep the current secret.')}
                </p>
              ) : null}
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditingProvider(null)}>
              {t('common_cancel', 'Cancel')}
            </Button>
            <Button
              disabled={oidcClientMutation.isPending
                || (editTokenAuthMethod !== 'none'
                  && !replacementClientSecret
                  && !editingProvider?.has_client_secret)}
              onClick={() => oidcClientMutation.mutate()}
            >
              {t('common_save', 'Save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={issuedToken !== null}
        onOpenChange={(open) => {
          if (!open) setIssuedToken(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <KeyRound className="size-5" />
              {t('organization.identity.tokenTitle', 'Save the SCIM token now')}
            </DialogTitle>
            <DialogDescription>
              {t('organization.identity.tokenDescription', 'This token is shown once. Store it in the identity provider secret field; Flowork keeps only its hash.')}
            </DialogDescription>
          </DialogHeader>
          <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
            <p className="mb-2 text-xs font-medium">{issuedToken?.providerName}</p>
            <code className="block break-all text-xs" data-testid="issued-scim-token">
              {issuedToken?.value}
            </code>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                if (issuedToken) void navigator.clipboard.writeText(issuedToken.value);
              }}
            >
              <Copy />
              {t('common.copy', 'Copy')}
            </Button>
            <Button onClick={() => setIssuedToken(null)}>
              {t('common.done', 'Done')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
