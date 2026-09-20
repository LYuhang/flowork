import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate, useParams, useSearchParams } from 'react-router';
import {
  CircleCheck,
  ExternalLink,
  LoaderCircle,
  RefreshCw,
  Router,
  Unplug,
} from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import {
  useCompleteOpenRouterConnection,
  useDisconnectOpenRouter,
  useOpenRouterConnection,
  useRefreshOpenRouterModels,
  useStartOpenRouterConnection,
} from '@/lib/api/queries/llm-credentials';

function isFreeModel(model: { id: string; pricing: { prompt?: string | null; completion?: string | null } }) {
  if (model.id === 'openrouter/free' || model.id.endsWith(':free')) return true;
  const prompt = Number(model.pricing.prompt);
  const completion = Number(model.pricing.completion);
  return Number.isFinite(prompt) && Number.isFinite(completion)
    && prompt === 0 && completion === 0;
}

function requestErrorCode(error: unknown): string | null {
  if (!error || typeof error !== 'object' || !('code' in error)) return null;
  const code = (error as { code?: unknown }).code;
  return typeof code === 'string' ? code : null;
}

export function OpenRouterConnectionPanel() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { openrouterState } = useParams<{ openrouterState?: string }>();
  const [searchParams] = useSearchParams();
  const connection = useOpenRouterConnection();
  const start = useStartOpenRouterConnection();
  const complete = useCompleteOpenRouterConnection();
  const refresh = useRefreshOpenRouterModels();
  const disconnect = useDisconnectOpenRouter();
  const callbackHandled = useRef(false);
  const [disconnectConfirm, setDisconnectConfirm] = useState(false);

  useEffect(() => {
    if (callbackHandled.current) return;
    const code = searchParams.get('code');
    const providerError = searchParams.get('error');
    const isCallback = Boolean(openrouterState || code || providerError);
    if (!isCallback) return;
    callbackHandled.current = true;
    if (providerError || !code || !openrouterState) {
      toast.error(t(
        'credentials.openrouter.callbackFailed',
        'OpenRouter did not complete the connection. Start again.',
      ));
      navigate('/settings?tab=api-keys', { replace: true });
      return;
    }
    void complete.mutateAsync({ code, state: openrouterState })
      .then(() => {
        toast.success(t(
          'credentials.openrouter.connectedToast',
          'OpenRouter connected',
        ));
      })
      .catch((error: unknown) => {
        const code = requestErrorCode(error);
        const message = code === 'openrouter_unreachable'
          ? t(
            'credentials.openrouter.callbackUnreachable',
            'Flowork could not reach OpenRouter. Check the deployment network or proxy, then start again.',
          )
          : code === 'openrouter_authorization_rejected'
            ? t(
              'credentials.openrouter.callbackRejected',
              'OpenRouter rejected or expired this authorization. Start again.',
            )
            : code === 'openrouter_state_invalid'
              ? t(
                'credentials.openrouter.callbackExpired',
                'This connection request has expired or was already used. Start again.',
              )
              : t(
                'credentials.openrouter.callbackFailed',
                'OpenRouter did not complete the connection. Start again.',
              );
        toast.error(message);
      })
      .finally(() => {
        navigate('/settings?tab=api-keys', { replace: true });
      });
  }, [complete, navigate, openrouterState, searchParams, t]);

  const beginConnection = async () => {
    try {
      const result = await start.mutateAsync();
      const target = new URL(result.authorization_url);
      if (
        target.protocol !== 'https:'
        || target.hostname !== 'openrouter.ai'
        || target.pathname !== '/auth'
      ) {
        throw new Error('invalid_openrouter_authorization_url');
      }
      window.location.assign(target.toString());
    } catch {
      toast.error(t(
        'credentials.openrouter.startFailed',
        'Could not start the OpenRouter connection.',
      ));
    }
  };

  const value = connection.data;
  const busy = start.isPending || complete.isPending;
  const availableModels = value?.models.filter((model) => model.available) ?? [];
  const freeModels = availableModels.filter(isFreeModel);

  return (
    <section
      className="overflow-hidden rounded-xl border border-sky-200/70 bg-gradient-to-br from-sky-50/70 via-background to-violet-50/40 dark:border-sky-900/60 dark:from-sky-950/20 dark:to-violet-950/10"
      data-testid="openrouter-connection"
    >
      <div className="flex flex-col gap-4 px-4 py-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 gap-3">
          <div className="grid size-10 shrink-0 place-items-center rounded-lg bg-sky-600 text-white shadow-sm shadow-sky-600/20">
            <Router className="size-5" aria-hidden="true" />
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold">OpenRouter</h3>
              {value?.connected ? (
                <span className="inline-flex items-center gap-1 text-xs font-medium text-emerald-700 dark:text-emerald-300">
                  <CircleCheck className="size-3.5" aria-hidden="true" />
                  {t('credentials.openrouter.connected', 'Connected')}
                </span>
              ) : null}
            </div>
            <p className="mt-1 max-w-[62ch] text-sm leading-6 text-muted-foreground">
              {t(
                'credentials.openrouter.description',
                'Connect your OpenRouter account to use the models allowed by your provider preferences and privacy settings. This does not sign you in to Flowork.',
              )}
            </p>
          </div>
        </div>
        <Button
          variant={value?.connected ? 'outline' : 'default'}
          className="shrink-0 self-start"
          disabled={busy}
          onClick={() => void beginConnection()}
        >
          {busy ? <LoaderCircle className="size-4 animate-spin" /> : <ExternalLink className="size-4" />}
          {value?.credential_id
            ? t('credentials.openrouter.reconnect', 'Reconnect OpenRouter')
            : t('credentials.openrouter.connect', 'Connect OpenRouter')}
        </Button>
      </div>

      {value?.credential_id ? (
        <div className="border-t border-sky-200/60 bg-background/55 px-4 py-4 dark:border-sky-900/50">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <p className="text-sm font-medium text-foreground">
                {t('credentials.openrouter.catalogReady', '{{count}} models available in Chat', {
                  count: availableModels.length,
                })}
              </p>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                {t('credentials.openrouter.catalogSummary', '{{freeCount}} free models. Choose the model and thinking level from the Chat composer when starting a conversation.', {
                  freeCount: freeModels.length,
                })}
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={refresh.isPending}
                onClick={() => void refresh.mutateAsync()}
              >
                <RefreshCw className={`size-4 ${refresh.isPending ? 'animate-spin' : ''}`} />
                {t('credentials.openrouter.refresh', 'Refresh models')}
              </Button>
              <Button
                variant={disconnectConfirm ? 'destructive' : 'ghost'}
                size="sm"
                disabled={disconnect.isPending}
                onClick={() => {
                  if (!disconnectConfirm) {
                    setDisconnectConfirm(true);
                    return;
                  }
                  void disconnect.mutateAsync().then(() => {
                    setDisconnectConfirm(false);
                    toast.success(t('credentials.openrouter.disconnected', 'OpenRouter disconnected'));
                  });
                }}
              >
                <Unplug className="size-4" />
                {disconnectConfirm
                  ? t('credentials.openrouter.confirmDisconnect', 'Confirm disconnect')
                  : t('credentials.openrouter.disconnect', 'Disconnect')}
              </Button>
            </div>
          </div>
          {value.models.length === 0 ? (
            <p className="mt-3 text-sm text-muted-foreground">
              {t('credentials.openrouter.emptyModels', 'No compatible text and tool-capable models are currently available. Refresh after updating your OpenRouter preferences.')}
            </p>
          ) : null}
          {value.error_code ? (
            <p className="mt-3 text-sm text-destructive" role="status">
              {value.error_code === 'openrouter_credentials_rejected'
                ? t('credentials.openrouter.rejected', 'OpenRouter rejected this key. Reconnect the account to continue.')
                : t('credentials.openrouter.refreshError', 'The model catalog could not be refreshed. The previous catalog has been kept.')}
            </p>
          ) : null}
          <p className="mt-3 text-xs leading-5 text-muted-foreground">
            {t('credentials.openrouter.boundary', 'Models and prices come from OpenRouter and may change. Flowork does not control billing or availability. Compatible models are available to the Runtime selected in Settings, including Codex through OpenRouter Responses API.')}
          </p>
        </div>
      ) : null}
    </section>
  );
}
