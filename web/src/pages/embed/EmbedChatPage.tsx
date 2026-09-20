/**
 * `/embed/chat` — the minimal route that renders ONLY the agent chat, for
 * framing inside the browser-extension side panel.
 *
 * It is intentionally NOT mounted under `AppLayout`: no topbar, no left nav, no
 * canvas — just the embedded `AgentChatSidebar`. The route is wrapped by the
 * app's `<Providers>` already (they sit above `RouterProvider` in `main.tsx`),
 * so QueryClient / Theme / Tooltip / Toaster are all available here.
 *
 * Two entry paths:
 *
 *   Entry A — from the main app: the URL already carries `wf` (the
 *     main app did the handoff and relayed a one-time exchange code). We skip the
 *     picker and the binding handshake and render the chat directly.
 *
 *   Entry B — cold side panel from any page: the URL has neither `wf` nor
 *     `browser`. The flow is:
 *       1. not authenticated → render the app's real login (`EmbedLogin`);
 *       2. authenticated → resolve the carrier scope and render Chat
 *          (reuses the workspace list + create-workflow surfaces);
 *       3. on pick/create → mint the scoped token, `postMessage` the shell to
 *          OPEN the WS + REQUEST its local control projection, await the
 *          shell's `{type:'BINDING', exchangeCode}` reply, then render Chat.
 *
 * Query parameters:
 *   - `wf`      — workflow id to bind the chat to (seeds `lastActiveWorkflowId`).
 *   - `chat`    — chat/session id to resume (seeds `activeChatId`; the sidebar
 *                 restores the latest browser Chat, or mints a fresh one when
 *                 no persisted browser Chat exists, if absent).
 *   - `mode`    — composer default mode (`browser` here); defaults to `browser`.
 * Authentication is cookie-based. The shell may relay a single-use exchange
 * code through BINDING; the iframe redeems it for a partitioned HttpOnly cookie.
 */
import { useCallback, useEffect, useState } from 'react';
import { ExternalLink, LoaderCircle, RotateCw } from 'lucide-react';
import { useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useTheme } from 'next-themes';
import { useUIStore } from '@/stores/ui';
import { useAuthStore } from '@/stores/auth';
import { useAgentSettingsStore } from '@/stores/agent-settings';
import { mintBrowserToken } from '@/lib/api/browser';
import { extensionOrigin } from '@/lib/extension';
import { useBrowserChatBootstrap, useChatSessions } from '@/lib/api/queries/chats';
import { cancelActiveTurn } from '@/lib/api/cancel-turn';
import { reconcileChatWithServer } from '@/lib/api/sse/chat-reconcile';
import { EmbedLogin } from '@/pages/embed/EmbedLogin';
import { EmbedShell } from '@/pages/embed/EmbedShell';
import { Button } from '@/components/ui/button';
import { getBasePath } from '@/lib/base-path';

/**
 * The main app's model settings, relayed through the extension
 * (handoff → storage → GET_BINDING/BINDING) so the embed runs with the same
 * credential and hyperparameters. Snake-case matches the SSE `agent_settings`
 * block.
 */
interface RelayedAgentSettings {
  model_id?: string;
  temperature?: number;
  max_tokens?: number;
  timeout?: number;
  reasoning_effort?: string;
}

/** The shell's reply to REQUEST_BINDING from extension to iframe. */
interface BindingMessage {
  type: 'BINDING';
  browser_id?: string;
  browser_control_chat_id?: string;
  browser_control_available_here?: boolean;
  /** A relayed chat_id to continue (legacy/defensive). Absent on entry B. */
  chat_id?: string;
  exchangeCode?: string;
  agentSettings?: RelayedAgentSettings;
}

function isBindingMessage(data: unknown): data is BindingMessage {
  return (
    typeof data === 'object' &&
    data !== null &&
    (data as { type?: unknown }).type === 'BINDING'
  );
}

/**
 * Seed `useAgentSettingsStore` from the relayed credential + hyperparameters so
 * the embed's SSE request resolves the same credential/model server-side as the
 * main-app page.
 */
function seedAgentSettings(s: RelayedAgentSettings): void {
  useAgentSettingsStore.getState().setAll({
    modelId: typeof s.model_id === 'string' ? s.model_id : null,
    temperature: typeof s.temperature === 'number' ? s.temperature : null,
    maxTokens: typeof s.max_tokens === 'number' ? s.max_tokens : null,
    timeout: typeof s.timeout === 'number' ? s.timeout : null,
    reasoningEffort:
      typeof s.reasoning_effort === 'string' && s.reasoning_effort.trim()
        ? s.reasoning_effort
        : null,
  });
}

export function EmbedChatPage() {
  const { t } = useTranslation();
  const { resolvedTheme } = useTheme();
  const [params] = useSearchParams();
  const wfParam = params.get('wf') ?? undefined;
  const modeParam = params.get('mode');
  const defaultMode: 'chat' | 'browser' =
    modeParam === 'chat' ? 'chat' : 'browser';
  const setLastWf = useUIStore((s) => s.setLastActiveWorkflowId);
  const setActiveChatId = useUIStore((s) => s.setActiveChatId);
  const authenticated = useAuthStore((s) => s.authenticated);
  const sessionAudience = useAuthStore((s) => s.sessionAudience);
  const bootstrap = useAuthStore((s) => s.bootstrap);
  const extensionAuthenticated =
    authenticated && sessionAudience === 'extension';

  const chatFromUrl = params.get('chat') ?? undefined;
  const [chat, setChat] = useState<string | undefined>(chatFromUrl);

  const [browserControlChatId, setBrowserControlChatId] = useState('');
  const [browserControlAvailableHere, setBrowserControlAvailableHere] = useState(false);
  const [browserId, setBrowserId] = useState('');

  // Gate the first render until the partitioned-cookie bootstrap
  // settles, so we don't flash the login pane for an authed embed.
  const [seeded, setSeeded] = useState(false);
  const [binding, setBinding] = useState(true);
  const [bindingFailed, setBindingFailed] = useState(false);
  const [boundWf, setBoundWf] = useState<string | null>(null);
  const browserBootstrap = useBrowserChatBootstrap(extensionAuthenticated);
  const wf = wfParam ?? browserBootstrap.data?.carrier_scope_id;
  // A post-tool Continue gate can outlive its originating Runtime Turn. On a
  // cold/reloaded Sidepanel there may therefore be no active Run to discover;
  // resolve the latest durable browser Chat before creating a new draft.
  const browserSessions = useChatSessions(
    seeded && extensionAuthenticated ? (wf ?? null) : null,
    'browser',
  );
  const trustedExtensionOrigin = extensionOrigin();

  const postToExtension = useCallback((message: unknown) => {
    if (!trustedExtensionOrigin || window.parent === window) return;
    window.parent.postMessage(message, trustedExtensionOrigin);
  }, [trustedExtensionOrigin]);

  // Keep the extension host shell in the same theme before subsequent iframe
  // paints. The shell persists this projection in chrome.storage, so its next
  // cold start can render without a light-theme flash while the React app loads.
  useEffect(() => {
    if (window.parent === window) return;
    if (resolvedTheme !== 'light' && resolvedTheme !== 'dark') return;
    postToExtension({ type: 'SET_THEME', theme: resolvedTheme });
  }, [postToExtension, resolvedTheme]);

  // Seed the workflow + chat binding into the UI store before mounting the
  // sidebar — `AgentChatSidebar` renders null until `lastActiveWorkflowId` is
  // set, and reads `activeChatId` for the conversation it resumes.
  useEffect(() => {
    if (wf) setLastWf(wf);
    if (chat) setActiveChatId('browser', chat);
  }, [wf, chat, setLastWf, setActiveChatId]);

  useEffect(() => {
    if (!wf || chat || !seeded || !extensionAuthenticated) return;
    const sessionsReady =
      browserSessions.data !== undefined ||
      browserSessions.isFetched ||
      browserSessions.isError;
    if (!sessionsReady) return;
    const latest = (
      browserSessions.data?.items as Array<{ chat_id?: string }> | undefined
    )?.find((item) => typeof item.chat_id === 'string' && item.chat_id)?.chat_id;
    const next = latest ?? crypto.randomUUID();
    let cancelled = false;
    queueMicrotask(() => {
      if (cancelled) return;
      setChat(next);
      setActiveChatId('browser', next);
    });
    return () => {
      cancelled = true;
    };
  }, [
    browserSessions.data,
    browserSessions.isError,
    browserSessions.isFetched,
    chat,
    seeded,
    setActiveChatId,
    extensionAuthenticated,
    wf,
  ]);

  // First check whether this iframe partition already owns an Extension
  // Session. A later BINDING can redeem a fresh one-time exchange code.
  useEffect(() => {
    let cancelled = false;
    const seed = async () => {
      await bootstrap();
      if (!cancelled) setSeeded(true);
    };
    void seed();
    return () => {
      cancelled = true;
    };
    // Run once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const bindToShell = useCallback(
    async (carrierWf: string, stableBrowserId: string) => {
      setBinding(true);
      setBindingFailed(false);
      try {
        const scopedToken = await mintBrowserToken(carrierWf, stableBrowserId);
        postToExtension({ type: 'OPEN_WS', scopedToken });
        postToExtension({ type: 'REQUEST_BINDING' });
      } catch {
        setBindingFailed(true);
      }
    },
    [postToExtension],
  );

  useEffect(() => {
    if (!seeded || !extensionAuthenticated || !wf || !browserId || boundWf === wf) return;
    queueMicrotask(() => {
      setBoundWf(wf);
      void bindToShell(wf, browserId);
    });
  }, [bindToShell, boundWf, browserId, extensionAuthenticated, seeded, wf]);

  // Listen for the shell's BINDING reply, redeem any fresh one-time exchange
  // code, and proceed to render the chat.
  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      // The extension side-panel shell is the only protocol peer. Do not accept
      // forged binding/session events posted by this frame or an unrelated
      // window; the shell separately validates this iframe's source + web origin.
      if (e.source !== window.parent) return;
      if (!trustedExtensionOrigin || e.origin !== trustedExtensionOrigin) return;
      if (
        typeof e.data === 'object' &&
        e.data !== null &&
        (e.data as { type?: unknown }).type === 'BROWSER_WS_AUTH_REQUIRED'
      ) {
        setBoundWf(null);
        setBinding(true);
        return;
      }
      if (
        typeof e.data === 'object' &&
        e.data !== null &&
        (e.data as { type?: unknown }).type === 'OPEN_WS_RESULT'
      ) {
        if ((e.data as { ok?: unknown }).ok === true) {
          setBindingFailed(false);
          setBinding(false);
        } else {
          setBindingFailed(true);
        }
        return;
      }
      if (
        typeof e.data === 'object' &&
        e.data !== null &&
        (e.data as { type?: unknown }).type === 'BROWSER_STOP_REQUESTED'
      ) {
        void (async () => {
          if (!wf || !chat) return;
          const cancelled = await cancelActiveTurn(chat);
          if (cancelled) {
            postToExtension(
              {
                type: 'BROWSER_TURN_CANCELLED',
                chat_id: chat,
                turn_id: cancelled.turnId,
              },
            );
          }
        })();
        return;
      }
      if (
        typeof e.data === 'object' &&
        e.data !== null &&
        (e.data as { type?: unknown }).type === 'BROWSER_SESSION_CHANGED'
      ) {
        const controlChatId =
          typeof (e.data as { browser_control_chat_id?: unknown }).browser_control_chat_id === 'string'
            ? (e.data as { browser_control_chat_id: string }).browser_control_chat_id
            : '';
        const availableHere =
          (e.data as { browser_control_available_here?: unknown }).browser_control_available_here === true;
        setBrowserControlChatId(controlChatId);
        setBrowserControlAvailableHere(availableHere);
        const status = (e.data as { status?: unknown }).status;
        if (status !== 'lost' && status !== 'released' && status !== 'inactive') {
          if (chat && wf) {
            void reconcileChatWithServer({ wfId: wf, chatId: chat, surface: 'browser' });
          }
          return;
        }
        if (chat && wf) {
          // The extension sends the fenced lifecycle event to the backend over
          // its own WebSocket and receives the durable ACK there. The embedded
          // UI only refreshes the resulting projection; it never forwards
          // browser session identity or mutates control state itself.
          void reconcileChatWithServer({
            wfId: wf,
            chatId: chat,
            surface: 'browser',
          });
        }
        return;
      }
      if (!isBindingMessage(e.data)) return;
      const received = e.data;
      void (async () => {
        if (received.exchangeCode) {
          await bootstrap(received.exchangeCode);
        }
        const auth = useAuthStore.getState();
        if (!auth.authenticated || auth.sessionAudience !== 'extension') {
          // A normal Web Session can also be visible to this iframe. It must
          // never race the one-time exchange and mint a browser token: browser
          // control is bound exclusively to the derived Extension Session.
          setBinding(true);
          postToExtension({ type: 'REQUEST_AUTH_REFRESH' });
          return;
        }
        if (received.exchangeCode) {
          postToExtension({ type: 'AUTH_EXCHANGE_CONSUMED' });
        }
        if (typeof received.browser_id === 'string' && received.browser_id) {
          setBrowserId(received.browser_id);
        }
        // Relayed agent settings (entry A and B): seed the credential/model
        // store only after the Extension Session is authoritative.
        if (received.agentSettings) {
          seedAgentSettings(received.agentSettings);
        }
        if (received.chat_id) setChat((prev) => prev ?? received.chat_id);
        setBrowserControlChatId(received.browser_control_chat_id || '');
        setBrowserControlAvailableHere(
          received.browser_control_available_here === true,
        );
        setBindingFailed(false);
        setBinding(false);
      })();
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [bootstrap, chat, postToExtension, trustedExtensionOrigin, wf]);

  // Cold side-panel authentication is a pull handshake, not a one-shot shell
  // push. The extension shell's iframe `load` event can fire before React has
  // installed the message listener above; a BINDING sent only at that moment is
  // lost and the embed deadlocks on its login screen even though the extension
  // may already have a valid partitioned Session. Once bootstrap has checked the
  // iframe partition and the listener is mounted, explicitly request the
  // authoritative binding. The shell response re-seeds auth through
  // `bootstrap(exchangeCode)` in the handler above. The extension retains the
  // code until this iframe ACKs a verified Session, so load-race replays are
  // safe and cannot lose authentication.
  useEffect(() => {
    if (!seeded || window.parent === window) return;
    postToExtension({ type: 'REQUEST_BINDING' });
  }, [postToExtension, seeded]);

  useEffect(() => {
    if (!seeded || extensionAuthenticated || window.parent === window) return;
    postToExtension({ type: 'REQUEST_AUTH_REFRESH' });
  }, [extensionAuthenticated, postToExtension, seeded]);

  useEffect(() => {
    if (!seeded || extensionAuthenticated) return;
    if (window.parent === window) {
      queueMicrotask(() => setBindingFailed(true));
      return;
    }
    const timer = window.setTimeout(() => setBindingFailed(true), 12_000);
    return () => window.clearTimeout(timer);
  }, [extensionAuthenticated, seeded]);

  useEffect(() => {
    if (!binding || !seeded || !extensionAuthenticated) return;
    const timer = window.setTimeout(() => setBindingFailed(true), 12_000);
    return () => window.clearTimeout(timer);
  }, [binding, extensionAuthenticated, seeded]);

  const retryBinding = useCallback(() => {
    setBinding(true);
    setBindingFailed(false);
    if (wf && browserId) {
      void bindToShell(wf, browserId);
      return;
    }
    postToExtension({ type: 'REQUEST_BINDING' });
  }, [bindToShell, browserId, postToExtension, wf]);

  if (!seeded) {
    return (
      <div className="flex h-screen items-center justify-center text-sm text-muted-foreground">
        {t('loading', 'Loading…')}
      </div>
    );
  }

  // Entry B step 1 — unauthenticated → render the app's real login inside the
  // embed (it receives a partitioned HttpOnly Session cookie).
  if (!authenticated) {
    return <EmbedLogin />;
  }

  if (!extensionAuthenticated && bindingFailed) {
    const appHref = `${getBasePath() || ''}/chat`;
    const standalone = window.parent === window;
    return (
      <div className="flex h-screen items-center justify-center bg-surface-app p-5">
        <section className="w-full max-w-sm rounded-xl border border-edge-structural bg-surface-raised p-5 text-center shadow-raised">
          <div className="mx-auto grid size-10 place-items-center rounded-xl bg-state-warning/10 text-state-warning">
            <RotateCw className="size-4" aria-hidden="true" />
          </div>
          <h1 className="mt-3 text-base font-semibold">
            {t('embed.session_timeout', 'The extension session could not be established')}
          </h1>
          <p className="mt-1 text-sm leading-5 text-muted-foreground">
            {standalone
              ? t('embed.standalone_hint', 'This secure Chat surface is opened by the Flowork browser extension. Open the side panel, or continue in the main app.')
              : t('embed.session_timeout_hint', 'Open the Flowork side panel and retry. If the extension was updated, reload it before trying again.')}
          </p>
          <div className="mt-4 flex flex-col gap-2 sm:flex-row sm:justify-center">
            {!standalone ? <Button size="sm" onClick={retryBinding}>
              <RotateCw className="size-3.5" aria-hidden="true" />
              {t('retry', 'Retry')}
            </Button> : null}
            <Button asChild size="sm" variant="outline">
              <a href={appHref} target="_blank" rel="noreferrer">
                <ExternalLink className="size-3.5" aria-hidden="true" />
                {t('embed.open_main_app', 'Open main app')}
              </a>
            </Button>
          </div>
        </section>
      </div>
    );
  }

  if (!extensionAuthenticated) {
    return (
      <div className="flex h-screen items-center justify-center gap-2 text-sm text-muted-foreground" role="status">
        <LoaderCircle className="size-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
        {t('embed.establishing_extension_session', 'Establishing the secure browser session…')}
      </div>
    );
  }

  if (browserBootstrap.isLoading || !wf || !chat) {
    return (
      <div className="flex h-screen items-center justify-center text-sm text-muted-foreground">
        {t('embed.preparing_browser_chat', 'Preparing browser chat…')}
      </div>
    );
  }

  if (binding && bindingFailed) {
    const appHref = `${getBasePath() || ''}/chat`;
    return (
      <div className="flex h-screen items-center justify-center bg-surface-app p-5">
        <section className="w-full max-w-sm rounded-xl border border-edge-structural bg-surface-raised p-5 text-center shadow-raised">
          <div className="mx-auto grid size-10 place-items-center rounded-xl bg-state-warning/10 text-state-warning">
            <RotateCw className="size-4" aria-hidden="true" />
          </div>
          <h1 className="mt-3 text-base font-semibold">
            {t('embed.binding_timeout', 'The browser connection is taking longer than expected')}
          </h1>
          <p className="mt-1 text-sm leading-5 text-muted-foreground">
            {t('embed.binding_timeout_hint', 'Make sure the browser side panel is open, then try connecting again.')}
          </p>
          <div className="mt-4 flex flex-col gap-2 sm:flex-row sm:justify-center">
            <Button size="sm" onClick={retryBinding}>
              <RotateCw className="size-3.5" aria-hidden="true" />
              {t('retry', 'Retry')}
            </Button>
            <Button asChild size="sm" variant="outline">
              <a href={appHref} target="_blank" rel="noreferrer">
                <ExternalLink className="size-3.5" aria-hidden="true" />
                {t('embed.open_main_app', 'Open main app')}
              </a>
            </Button>
          </div>
        </section>
      </div>
    );
  }

  if (binding) {
    return (
      <div className="flex h-screen items-center justify-center gap-2 text-sm text-muted-foreground" role="status">
        <LoaderCircle className="size-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
        {t('embed.binding', 'Connecting to the browser…')}
      </div>
    );
  }

  return (
    <EmbedShell
      wfId={wf}
      defaultMode={defaultMode}
      browserControlChatId={browserControlChatId}
      browserControlAvailableHere={browserControlAvailableHere}
      browserOnly
    />
  );
}
