import { describe, expect, it, vi, beforeEach } from 'vitest';
import { StrictMode } from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { ChatComposer } from '@/components/agent-sidebar/ChatComposer';
import { useChatStreamStore } from '@/stores/chat-stream';
import { useAgentSettingsStore } from '@/stores/agent-settings';
import { useChatAgentSettingsStore } from '@/stores/chat-agent-settings';
import { useAuthStore } from '@/stores/auth';
import { chatClientStateKey } from '@/lib/chat/state-key';
import { server } from '@/__tests__/msw-handlers';

function composerKey(chatId: string): string {
  return chatClientStateKey({
    account: useAuthStore.getState().user,
    scopeId: 'wf_x',
    surface: 'chat',
    chatId,
  });
}

function renderComposer(
  chatId: string,
  showModelSelector = false,
  onSendStart?: () => void,
  embedded = false,
  strict = false,
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const component = (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ChatComposer
          wfId="wf_x"
          chatId={chatId}
          embedded={embedded}
          showModelSelector={showModelSelector}
          onSendStart={onSendStart}
        />
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(strict ? <StrictMode>{component}</StrictMode> : component);
}

describe('ChatComposer Stop', () => {
  let cancelled: Array<{ chatId: string; turnId: string }>;

  beforeEach(() => {
    cancelled = [];
    server.use(
      http.get('*/api/v1/chats/bootstrap', () => HttpResponse.json({
        carrier_scope_id: 'wf_x',
        surface: 'chat',
        available_commands: [],
        debug_view_enabled: false,
      })),
      http.post('*/api/v1/chats/:chatId/active-turn/cancel', ({ params }) => {
        cancelled.push({
          chatId: String(params.chatId),
          turnId: 'turn_a',
        });
        return HttpResponse.json(
          {
            status: 'cancel-requested',
            chat_id: String(params.chatId),
            run_id: 'turn_a',
          },
          { status: 202 },
        );
      }),
    );
    useChatStreamStore.getState().reset();
    useAgentSettingsStore.getState().reset();
    useChatAgentSettingsStore.setState({ entries: {} });
  });

  it('shows and stores the concrete API selection instead of a synthetic Default', async () => {
    const modelId = 'langchain:credential:11111111-1111-4111-8111-111111111111';
    server.use(
      http.get('*/api/v1/agent-runtime/capabilities', () => HttpResponse.json({
        protocol_version: 2,
        runtime_type: 'langchain',
        runtime_available: true,
        authenticated: true,
        source: 'test-explicit-api',
        models: [{
          id: modelId,
          label: 'Test account API',
          description: 'openai · gpt-test',
          provider: 'openai',
          is_default: false,
          supported_reasoning_efforts: [],
          default_reasoning_effort: null,
        }],
        default_model_id: modelId,
        error_code: null,
        bound_agent_settings: null,
      })),
    );

    const { container } = renderComposer('chat_explicit_api', true);

    await waitFor(() => {
      expect(container.querySelector('[data-role="chat-model-select"]')).toHaveTextContent(
        'Test account API',
      );
      expect(
        useChatAgentSettingsStore.getState().entries.chat_explicit_api?.settings.modelId,
      ).toBe(modelId);
    });
    expect(screen.queryByText(/Runtime default/i)).toBeNull();
  });

  it('navigates Codex account and API sources before choosing a model', async () => {
    const user = userEvent.setup();
    server.use(
      http.get('*/api/v1/agent-runtime/capabilities', () => HttpResponse.json({
        protocol_version: 2,
        runtime_type: 'codex',
        runtime_available: true,
        authenticated: true,
        source: 'test-codex-connections',
        models: [
          {
            id: 'codex:account:gpt-5.6-sol',
            label: 'GPT-5.6-Sol',
            description: 'Connected OpenAI account',
            provider: 'chatgpt',
            is_default: true,
            supported_reasoning_efforts: [],
            default_reasoning_effort: null,
          },
          {
            id: 'codex:credential:11111111-1111-4111-8111-111111111111',
            label: 'Production OpenAI',
            description: 'openai · gpt-5.2-codex',
            provider: 'openai',
            is_default: false,
            supported_reasoning_efforts: [],
            default_reasoning_effort: null,
          },
        ],
        default_model_id: 'codex:account:gpt-5.6-sol',
        error_code: null,
        bound_agent_settings: null,
      })),
    );

    renderComposer('chat_codex_model_groups', true);
    const picker = await screen.findByRole('button', { name: 'Model' });
    await user.click(picker);

    expect(screen.getByText('OpenAI account')).toBeInTheDocument();
    expect(screen.getByText('My API connections')).toBeInTheDocument();
    await user.click(screen.getByText('OpenAI account'));
    expect(screen.getByRole('button', { name: /GPT-5.6-Sol/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Production OpenAI/ })).toBeNull();
  });

  it('searches an OpenRouter free model and exposes only its reasoning levels', async () => {
    const user = userEvent.setup();
    const accountModel = 'codex:account:gpt-5.6-sol';
    const openRouterModel = 'codex:openrouter:11111111-1111-4111-8111-111111111111:b3gtYWxwaGE';
    server.use(
      http.get('*/api/v1/agent-runtime/capabilities', () => HttpResponse.json({
        protocol_version: 2,
        runtime_type: 'codex',
        runtime_available: true,
        authenticated: true,
        source: 'test-openrouter-codex',
        models: [
          {
            id: accountModel,
            label: 'GPT-5.6-Sol',
            description: 'Connected OpenAI account',
            api_source: 'chatgpt_account',
            api_protocol: 'codex_app_server',
            provider: 'chatgpt',
            provider_model_id: 'gpt-5.6-sol',
            is_default: true,
            supported_reasoning_efforts: [{ id: 'high', label: 'High', description: '' }],
            default_reasoning_effort: null,
          },
          {
            id: openRouterModel,
            label: 'Ox Alpha',
            description: 'Free agent model',
            api_source: 'openrouter_oauth',
            api_protocol: 'openai_responses',
            provider: 'openrouter',
            provider_model_id: 'stealth/ox-alpha',
            context_length: 1048576,
            supports_tools: true,
            input_price: '0',
            output_price: '0',
            is_default: false,
            supported_reasoning_efforts: [
              { id: 'low', label: 'Low', description: '' },
              { id: 'high', label: 'High', description: '' },
              { id: 'max', label: 'Maximum', description: '' },
            ],
            default_reasoning_effort: 'max',
          },
        ],
        default_model_id: accountModel,
        error_code: null,
        bound_agent_settings: null,
      })),
    );

    renderComposer('chat_openrouter_model', true);
    await user.click(await screen.findByRole('button', { name: 'Model' }));
    expect(screen.getByText(/1 free/)).toBeInTheDocument();
    await user.click(screen.getByText('OpenRouter'));
    const search = screen.getByPlaceholderText(/Search models, providers, or free/i);
    await user.type(search, 'free');
    expect(await screen.findByRole('button', { name: /Ox Alpha/ })).toHaveTextContent('stealth/ox-alpha');
    await user.click(screen.getByRole('button', { name: /Ox Alpha/ }));

    expect(
      useChatAgentSettingsStore.getState().entries.chat_openrouter_model?.settings.modelId,
    ).toBe(openRouterModel);
    await user.click(screen.getByRole('button', { name: 'Options' }));
    const thinking = screen.getByRole('combobox', { name: 'Thinking' });
    expect(thinking).toBeEnabled();
    await user.click(thinking);
    expect(screen.getByRole('option', { name: 'Low' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'High' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Maximum' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Medium' })).toBeNull();
  });

  it('sends and resumes the same model selection contract in the side panel', async () => {
    const user = userEvent.setup();
    const accountModel = 'codex:account:gpt-5.6-sol';
    const openRouterModel = 'codex:openrouter:11111111-1111-4111-8111-111111111111:b3gtYWxwaGE';
    let requestBody: Record<string, unknown> | null = null;
    let bound = false;
    server.use(
      http.get('*/api/v1/chats/bootstrap', () => HttpResponse.json({
        carrier_scope_id: 'wf_x',
        surface: 'browser',
        available_commands: [],
        debug_view_enabled: false,
      })),
      http.get('*/api/v1/agent-runtime/capabilities', () => HttpResponse.json({
        protocol_version: 2,
        runtime_type: 'codex',
        runtime_available: true,
        authenticated: true,
        source: 'test-sidepanel-openrouter',
        models: [
          ...(!bound ? [
          {
            id: accountModel,
            label: 'GPT-5.6-Sol',
            api_source: 'chatgpt_account',
            provider_model_id: 'gpt-5.6-sol',
            supported_reasoning_efforts: [],
            default_reasoning_effort: null,
          },
          ] : []),
          {
            id: openRouterModel,
            label: 'Ox Alpha',
            api_source: 'openrouter_oauth',
            provider_model_id: 'stealth/ox-alpha',
            supports_tools: true,
            input_price: '0',
            output_price: '0',
            supported_reasoning_efforts: [
              { id: 'low', label: 'Low', description: '' },
              { id: 'max', label: 'Maximum', description: '' },
            ],
            default_reasoning_effort: 'max',
          },
        ],
        default_model_id: bound ? openRouterModel : accountModel,
        bound_agent_settings: bound ? {
          model_id: openRouterModel,
          temperature: null,
          max_tokens: null,
          timeout: null,
          reasoning_effort: 'max',
        } : null,
        error_code: null,
      })),
      http.get('*/api/v1/chat-scopes/wf_x/chats/chat_sidepanel_model/state', () =>
        HttpResponse.json({
          todo_items: [],
          background_jobs: [],
          active_modes: [],
          mcp_server_ids: [],
          mcp_config_revision: 0,
        })),
      http.post(
        '*/api/v1/chat-scopes/wf_x/chats/chat_sidepanel_model/messages',
        async ({ request }) => {
          requestBody = await request.json() as Record<string, unknown>;
          bound = true;
          return new HttpResponse('id: 1\nevent: done\ndata: {}\n\n', {
            headers: {
              'Content-Type': 'text/event-stream',
              'X-Turn-Id': 'turn_sidepanel_model',
            },
          });
        },
      ),
    );

    const first = renderComposer('chat_sidepanel_model', true, undefined, true);
    await user.click(await screen.findByRole('button', { name: 'Model' }));
    await user.click(screen.getByText('OpenRouter'));
    await user.click(screen.getByRole('button', { name: /Ox Alpha/ }));
    await user.click(screen.getByRole('button', { name: 'Options' }));
    await user.click(screen.getByRole('combobox', { name: 'Thinking' }));
    await user.click(screen.getByRole('option', { name: 'Maximum' }));
    await user.type(screen.getByRole('textbox'), 'side panel model switch');
    await user.click(screen.getByRole('button', { name: /send|发送/i }));

    await waitFor(() => expect(requestBody).not.toBeNull());
    expect(requestBody).toMatchObject({
      surface: 'sidepanel',
      agent_surface: 'browser',
      agent_settings: {
        model_id: openRouterModel,
        reasoning_effort: 'max',
      },
    });

    first.unmount();
    useChatAgentSettingsStore.setState({ entries: {} });
    const resumed = renderComposer('chat_sidepanel_model', true, undefined, true);
    await waitFor(() => {
      expect(resumed.container.querySelector('[data-role="chat-model-select"]'))
        .toHaveTextContent('Ox Alpha');
    });
    await user.click(resumed.container.querySelector(
      '[data-role="chat-composer-options-toggle"]',
    ) as HTMLElement);
    expect(resumed.container.querySelector('[data-role="chat-reasoning-effort-select"]'))
      .toHaveTextContent('Maximum');
    await user.click(resumed.container.querySelector(
      '[data-role="chat-model-select"]',
    ) as HTMLElement);
    expect(screen.getByText('OpenRouter')).toBeInTheDocument();
    expect(screen.queryByText('OpenAI account')).not.toBeInTheDocument();
  });

  it('does not infer free pricing from a manual connection model id', async () => {
    const user = userEvent.setup();
    server.use(
      http.get('*/api/v1/agent-runtime/capabilities', () => HttpResponse.json({
        runtime_type: 'codex',
        runtime_available: true,
        authenticated: true,
        source: 'test',
        default_model_id: 'codex:credential:manual-free-looking',
        models: [{
          id: 'codex:credential:manual-free-looking',
          label: 'My free-looking model',
          api_source: 'manual',
          api_protocol: 'openai_responses',
          provider: 'openai',
          provider_model_id: 'vendor/model:free',
          input_price: '0',
          output_price: '0',
          available: true,
          supported_reasoning_efforts: [],
        }],
      })),
      http.get(
        '*/api/v1/chat-scopes/wf_x/chats/chat_manual_free_looking/state',
        () => HttpResponse.json({ todo_items: [], active_command_ids: [] }),
      ),
    );

    renderComposer('chat_manual_free_looking', true);
    await user.click(await screen.findByTestId('chat-model-select'));
    await user.click(screen.getByRole('button', { name: /My API connections/i }));

    expect(screen.getByRole('button', { name: /My free-looking model/ }))
      .toBeInTheDocument();
    expect(screen.queryByText('Free')).not.toBeInTheDocument();
  });

  it('hydrates the server-bound model and allows switching within that connection', async () => {
    const accountModelId = 'codex:account:gpt-5.6-sol';
    const alternateAccountModelId = 'codex:account:gpt-5.5-codex';
    server.use(
      http.get('*/api/v1/agent-runtime/capabilities', () => HttpResponse.json({
        protocol_version: 2,
        runtime_type: 'codex',
        runtime_available: true,
        authenticated: true,
        source: 'test-bound-codex-connection',
        models: [
          {
            id: accountModelId,
            label: 'GPT-5.6-Sol',
            description: 'Connected OpenAI account',
            provider: 'chatgpt',
            is_default: true,
            supported_reasoning_efforts: [],
            default_reasoning_effort: null,
          },
          {
            id: alternateAccountModelId,
            label: 'GPT-5.5 Codex',
            description: 'Connected OpenAI account',
            provider: 'chatgpt',
            api_source: 'chatgpt_account',
            is_default: false,
            supported_reasoning_efforts: [],
            default_reasoning_effort: null,
          },
        ],
        default_model_id: accountModelId,
        error_code: null,
        bound_agent_settings: {
          model_id: accountModelId,
          temperature: null,
          max_tokens: null,
          timeout: null,
          reasoning_effort: null,
        },
      })),
    );

    const { container } = renderComposer('chat_bound_codex_model', true);
    const picker = await screen.findByRole('button', { name: 'Model' });

    await waitFor(() => {
      expect(picker).toBeEnabled();
      expect(
        useChatAgentSettingsStore.getState().entries.chat_bound_codex_model,
      ).toMatchObject({
        settings: { modelId: accountModelId },
      });
    });

    await userEvent.click(picker);
    await userEvent.click(screen.getByText('OpenAI account'));
    await userEvent.click(screen.getByRole('button', { name: /GPT-5.5 Codex/ }));

    expect(
      useChatAgentSettingsStore.getState().entries.chat_bound_codex_model?.settings.modelId,
    ).toBe(alternateAccountModelId);
    expect(container.querySelector('[data-role="chat-model-select"]'))
      .toHaveTextContent('GPT-5.5 Codex');
  });

  it('preserves an unavailable explicit API instead of switching to another one', async () => {
    const unavailableId = 'langchain:credential:22222222-2222-4222-8222-222222222222';
    const otherId = 'langchain:credential:33333333-3333-4333-8333-333333333333';
    useAgentSettingsStore.getState().set({ modelId: unavailableId });
    server.use(
      http.get('*/api/v1/agent-runtime/capabilities', () => HttpResponse.json({
        protocol_version: 2,
        runtime_type: 'langchain',
        runtime_available: true,
        authenticated: true,
        source: 'test-explicit-api',
        models: [{
          id: otherId,
          label: 'Different API',
          description: 'openai · gpt-other',
          provider: 'openai',
          is_default: false,
          supported_reasoning_efforts: [],
          default_reasoning_effort: null,
        }],
        default_model_id: otherId,
        error_code: null,
        bound_agent_settings: null,
      })),
    );

    renderComposer('chat_stale_api', true);

    await waitFor(() => {
      expect(
        useChatAgentSettingsStore.getState().entries.chat_stale_api?.settings.modelId,
      ).toBe(unavailableId);
    });
    expect(screen.getByText(/unavailable/i)).toBeInTheDocument();
    expect(useChatAgentSettingsStore.getState().entries.chat_stale_api?.settings.modelId)
      .not.toBe(otherId);
  });

  it('clears a model selection inherited from a different Runtime for a new draft', async () => {
    const langChainModelId = 'langchain:credential:44444444-4444-4444-8444-444444444444';
    useAgentSettingsStore.getState().set({ modelId: langChainModelId });
    server.use(
      http.get('*/api/v1/agent-runtime/capabilities', () => HttpResponse.json({
        protocol_version: 2,
        runtime_type: 'codex',
        runtime_available: true,
        authenticated: false,
        source: 'test-runtime-isolation',
        models: [],
        default_model_id: null,
        error_code: 'connection_required',
        bound_agent_settings: null,
      })),
    );

    const { container } = renderComposer('chat_cross_runtime_default', true);

    await waitFor(() => {
      expect(
        useChatAgentSettingsStore.getState().entries.chat_cross_runtime_default?.settings.modelId,
      ).toBeNull();
    });
    expect(container.querySelector('[data-role="chat-model-select"]'))
      .toHaveTextContent('No model configured');
    expect(screen.queryByText(new RegExp(langChainModelId))).toBeNull();
  });

  it('waits for the persisted chat row before reading chat state', async () => {
    let stateReads = 0;
    server.use(
      http.get('*/api/v1/chat-scopes/wf_x/chats/chat_pending/state', () => {
        stateReads += 1;
        return HttpResponse.json({
          todo_items: [],
          background_jobs: [],
          active_modes: [],
          mcp_server_ids: [],
          mcp_config_revision: 0,
        });
      }),
    );
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const view = render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ChatComposer wfId="wf_x" chatId="chat_pending" chatStateReady={false} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(stateReads).toBe(0);

    view.rerender(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ChatComposer wfId="wf_x" chatId="chat_pending" chatStateReady />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(stateReads).toBe(1));
  });

  it('keeps MCP selection available before a draft chat state is materialized', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const { container } = render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ChatComposer
            wfId="wf_x"
            chatId="chat_draft"
            chatStateReady={false}
            showModelSelector
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await userEvent.click(
      container.querySelector('[data-role="chat-composer-options-toggle"]') as HTMLElement,
    );
    await waitFor(() => {
      expect(container.querySelector('[data-role="chat-mcp-picker"]')).toBeEnabled();
    });
  });

  it('does not advertise or resend an uninstalled MCP selection', async () => {
    server.use(
      http.get('*/api/v1/chat-scopes/wf_x/chats/chat_uninstalled_mcp/state', () =>
        HttpResponse.json({
          todo_items: [],
          background_jobs: [],
          active_modes: [],
          mcp_server_ids: ['aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'],
          mcp_config_revision: 4,
        })),
      http.get('*/api/v1/mcp-servers', () => HttpResponse.json({ items: [] })),
    );

    const { container } = renderComposer('chat_uninstalled_mcp', true);
    await userEvent.click(
      container.querySelector('[data-role="chat-composer-options-toggle"]') as HTMLElement,
    );

    await waitFor(() => {
      expect(container.querySelector('[data-role="chat-mcp-picker"]')).toHaveTextContent(/^MCP$/);
    });
    expect(container.querySelector('[data-role="chat-composer-options-toggle"]'))
      .not.toHaveTextContent('1');
  });

  it('sends a chat-scoped backend cancel request without aborting or rewriting messages', async () => {
    const abort = vi.fn();
    useChatStreamStore.setState({
      chatId: 'chat_a',
      turnId: 'turn_a',
      state: 'streaming',
      abortController: { abort } as unknown as AbortController,
      buffer: [
        { role: 'user', content: 'run it' },
        {
          role: 'assistant',
          content: '',
          tool_calls: [{ id: 'tc1', name: 'bash', arguments: '{}' }],
        },
      ] as never,
    });

    renderComposer('chat_a');

    await userEvent.click(screen.getByRole('button', { name: /stop|停止/i }));

    await waitFor(() => {
      expect(cancelled).toEqual([{ chatId: 'chat_a', turnId: 'turn_a' }]);
    });
    expect(abort).not.toHaveBeenCalled();
    const s = useChatStreamStore.getState();
    expect(s.state).toBe('streaming');
    expect(s.buffer).toHaveLength(2);
  });

  it('consumes an onboarding draft exactly once under Strict Mode and preserves existing text', async () => {
    useChatStreamStore.getState().setComposerInput(composerKey('chat_onboarding'), 'Existing draft');
    useChatStreamStore.getState().setDraft('/workflow Make images', 'chat_onboarding');
    renderComposer('chat_onboarding', false, undefined, false, true);
    await waitFor(() => expect(screen.getByRole('textbox')).toHaveValue('Existing draft\n\n/workflow Make images'));
    expect(useChatStreamStore.getState().draft).toBeNull();
  });

  it('keeps a follow-up draft editable while running without submitting or stopping', async () => {
    const user = userEvent.setup();
    useChatStreamStore.getState().beginTurn('chat_drafting', 'turn_drafting');
    renderComposer('chat_drafting');
    const input = screen.getByRole('textbox');
    expect(input).toBeEnabled();
    await user.type(input, 'Keep the next image green');
    await user.keyboard('{Control>}{Enter}{/Control}');
    expect(input).toHaveValue('Keep the next image green');
    expect(cancelled).toEqual([]);
    expect(screen.getByRole('button', { name: /stop|停止/i })).toBeEnabled();
    act(() => useChatStreamStore.getState().setState('complete', 'chat_drafting'));
    expect(input).toHaveValue('Keep the next image green');
    expect(screen.getByRole('button', { name: /send|发送/i })).toBeEnabled();
  });

  it('lets a new draft take priority over retry after cancellation', async () => {
    useChatStreamStore.getState().beginTurn('chat_new_draft', 'turn_old');
    useChatStreamStore.getState().setLastInput({ content: 'old request' }, 'chat_new_draft');
    useChatStreamStore.getState().setState('cancelled', 'chat_new_draft');
    renderComposer('chat_new_draft');
    expect(screen.getByRole('button', { name: /retry|重试/i })).toBeInTheDocument();
    await userEvent.type(screen.getByRole('textbox'), 'different request');
    expect(screen.getByRole('button', { name: /send|发送/i })).toBeEnabled();
    expect(screen.queryByRole('button', { name: /retry|重试/i })).not.toBeInTheDocument();
  });

  it('offers Retry after the backend confirms a cancelled turn', () => {
    useChatStreamStore.getState().beginTurn('chat_cancelled', 'turn_cancelled');
    useChatStreamStore.getState().setLastInput({ content: 'run it again' }, 'chat_cancelled');
    useChatStreamStore.getState().setState('cancelled', 'chat_cancelled');

    renderComposer('chat_cancelled');

    expect(screen.getByRole('button', { name: /retry|重试/i })).toBeEnabled();
    expect(screen.queryByRole('button', { name: /stop|停止/i })).toBeNull();
  });

  it('resolves the Run from the backend when the local projection has no Turn id', async () => {
    useChatStreamStore.setState({
      runtimes: {
        chat_a: {
          chatId: 'chat_a',
          turnId: null,
          projectionActive: true,
          state: 'streaming',
          buffer: [],
          messages: [],
          todoItems: null,
          abortController: null,
          lastInput: null,
          startupPhase: null,
          startupProgress: null,
          waitingForUser: false,
        },
      },
    });

    renderComposer('chat_a');

    await userEvent.click(screen.getByRole('button', { name: /stop|停止/i }));

    await waitFor(() => {
      expect(cancelled).toEqual([{ chatId: 'chat_a', turnId: 'turn_a' }]);
    });
    expect(useChatStreamStore.getState().runtimes.chat_a.state).toBe('streaming');
  });

  it('does not expose another chat turn as this composer Stop action', async () => {
    useChatStreamStore.setState({
      chatId: 'chat_a',
      turnId: 'turn_a',
      state: 'streaming',
    });

    renderComposer('chat_b');

    const input = screen.getByRole('textbox');
    await userEvent.type(input, 'hello from b');

    expect(screen.queryByRole('button', { name: /stop|停止/i })).toBeNull();
    expect(screen.getByRole('button', { name: /send|发送/i })).toBeEnabled();
  });

  it('moves text and attachments from the composer into the user message before server acceptance', async () => {
    let releaseRequest: (() => void) | undefined;
    const requestGate = new Promise<void>((resolve) => {
      releaseRequest = resolve;
    });
    server.use(
      http.get('*/api/v1/chat-scopes/wf_x/chats/chat_optimistic/state', () =>
        HttpResponse.json({
          todo_items: [],
          background_jobs: [],
          active_modes: [],
          mcp_server_ids: [],
          mcp_config_revision: 0,
        })),
      http.post(
        '*/api/v1/chat-scopes/wf_x/chats/chat_optimistic/messages',
        async () => {
          await requestGate;
          return new HttpResponse('id: 1\nevent: done\ndata: {}\n\n', {
            headers: {
              'Content-Type': 'text/event-stream',
              'X-Turn-Id': 'turn_optimistic',
            },
          });
        },
      ),
    );

    const attachment = {
      type: 'file' as const,
      name: 'customer-feedback.csv',
      path: '/data/attachments/customer-feedback.csv',
      content_type: 'text/csv',
      size_bytes: 128,
    };
    const stateKey = composerKey('chat_optimistic');
    useChatStreamStore.getState().addAttachment(stateKey, attachment);
    const onSendStart = vi.fn(() => {
      // The visual clear is committed before the empty-chat shell is allowed
      // to switch to the optimistic transcript.
      expect(input).toHaveValue('');
      expect(container.querySelectorAll('[data-role="agent-composer-attachment-chip"]'))
        .toHaveLength(0);
    });
    const { container } = renderComposer('chat_optimistic', false, onSendStart);
    const input = screen.getByRole('textbox');
    expect(container.querySelectorAll('[data-role="agent-composer-attachment-chip"]'))
      .toHaveLength(1);
    await userEvent.type(input, 'show this immediately');
    await userEvent.click(screen.getByRole('button', { name: /send|发送/i }));

    // The page must switch from its empty shell to the transcript while the
    // request is still blocked, so network setup time is represented by the
    // optimistic user bubble and Agent thinking state.
    expect(onSendStart).toHaveBeenCalledTimes(1);
    expect(input).toHaveValue('');
    expect(input).toHaveAttribute('placeholder', 'The Agent is working. Draft your next message…');
    expect(input).toBeEnabled();
    expect(container.querySelectorAll('[data-role="agent-composer-attachment-chip"]'))
      .toHaveLength(0);
    expect(useChatStreamStore.getState().pendingAttachments[stateKey])
      .toBeUndefined();
    expect(useChatStreamStore.getState().runtimes.chat_optimistic.buffer).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          role: 'user',
          content: 'show this immediately',
          attachments: [attachment],
        }),
      ]),
    );
    expect(useChatStreamStore.getState().runtimes.chat_optimistic.messages[0])
      .toMatchObject({
        role: 'user',
        content: 'show this immediately',
        attachments: [attachment],
      });

    releaseRequest?.();
    await waitFor(() => {
      expect(useChatStreamStore.getState().runtimes.chat_optimistic.turnId)
        .toBe('turn_optimistic');
    });
  });

  it('restores text and attachments when the backend rejects the turn before acceptance', async () => {
    server.use(
      http.get('*/api/v1/chat-scopes/wf_x/chats/chat_rejected/state', () =>
        HttpResponse.json({
          todo_items: [],
          background_jobs: [],
          active_modes: [],
          mcp_server_ids: [],
          mcp_config_revision: 0,
        })),
      http.post(
        '*/api/v1/chat-scopes/wf_x/chats/chat_rejected/messages',
        () => HttpResponse.json(
          { detail: { code: 'chat_run_active' } },
          { status: 409 },
        ),
      ),
      http.get(
        '*/api/v1/chats/chat_rejected/turns/by-client-request/:clientRequestId',
        () => HttpResponse.json({ detail: 'not found' }, { status: 404 }),
      ),
    );
    const attachment = {
      type: 'file' as const,
      name: 'support-playbook.md',
      path: '/data/attachments/support-playbook.md',
      content_type: 'text/markdown',
      size_bytes: 256,
    };
    const stateKey = composerKey('chat_rejected');
    useChatStreamStore.getState().addAttachment(stateKey, attachment);

    const { container } = renderComposer('chat_rejected');
    const input = screen.getByRole('textbox');
    await userEvent.type(input, 'keep this draft');
    await userEvent.click(screen.getByRole('button', { name: /send|发送/i }));

    await waitFor(() => {
      expect(input).toHaveValue('keep this draft');
      expect(container.querySelectorAll('[data-role="agent-composer-attachment-chip"]'))
        .toHaveLength(1);
    });
    expect(useChatStreamStore.getState().pendingAttachments[stateKey])
      .toEqual([attachment]);
  });

  it('keeps inline controls transparent while a turn disables them', async () => {
    useChatStreamStore.getState().beginTurn('chat_a', 'turn_a');

    const { container } = renderComposer('chat_a', true);

    await waitFor(() => {
      expect(container.querySelector('[data-role="chat-model-select"]')).toBeDisabled();
    });
    expect(container.querySelector('[data-role="chat-approval-mode-select"]')).toBeNull();
    for (const role of [
      'chat-model-select',
      'chat-reasoning-effort-select',
    ]) {
      const trigger = container.querySelector(`[data-role="${role}"]`);
      if (!trigger) continue;
      expect(trigger).toHaveClass('disabled:bg-transparent');
      expect(trigger).toBeDisabled();
    }
  });

  it('does not render active commands as composer attachment chips', async () => {
    server.use(
      http.get('*/api/v1/chats/bootstrap', () => HttpResponse.json({
        carrier_scope_id: 'wf_x',
        surface: 'chat',
        available_commands: ['workflow', 'knowledge'],
      })),
      http.get('*/api/v1/chat-scopes/wf_x/chats/chat_codex/state', () => HttpResponse.json({
        todo_items: [],
        background_jobs: [],
        active_modes: ['plan'],
        mcp_server_ids: [],
        mcp_config_revision: 0,
      })),
      http.get('*/api/v1/agent-runtime/capabilities', () => HttpResponse.json({
        protocol_version: 1,
        runtime_type: 'codex',
        runtime_available: true,
        authenticated: true,
        source: 'chat-binding',
        models: [],
        default_model_id: null,
        bound_agent_settings: null,
        error_code: null,
      })),
    );

    const { container } = renderComposer('chat_codex', true);

    await waitFor(() => expect(screen.getByRole('textbox')).toBeEnabled());
    expect(container.querySelector('[data-role="active-command-list"]')).not.toBeInTheDocument();
    expect(container.querySelector('[data-command="workflow"]')).not.toBeInTheDocument();
  });

});
