/**
 * `CredentialsSettingsPanel` and `CredentialRow` smoke tests.
 *
 * Mocks the API client module (`@/lib/api/llm-credentials`) so the REAL
 * react-query hooks run end-to-end. Asserts:
 *   - the list renders one row per credential (public fields)
 *   - the api_key remains permanently masked
 *   - no reveal/copy control exists because provider secrets are write-only
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
} from 'react-router';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import i18n from 'i18next';
import en from '@/lib/i18n/locales/en.json';
import zh from '@/lib/i18n/locales/zh.json';

const {
  testConnectionMock,
  openRouterStatusMock,
  refreshOpenRouterMock,
  disconnectOpenRouterMock,
  completeOpenRouterMock,
  toastErrorMock,
  toastSuccessMock,
} = vi.hoisted(() => ({
  testConnectionMock: vi.fn(async () => ({
    ok: true,
    outcome: 'connected' as const,
    latency_ms: 42,
    upstream_status: 200,
  })),
  openRouterStatusMock: vi.fn(),
  refreshOpenRouterMock: vi.fn(),
  disconnectOpenRouterMock: vi.fn(),
  completeOpenRouterMock: vi.fn(),
  toastErrorMock: vi.fn(),
  toastSuccessMock: vi.fn(),
}));

vi.mock('@/lib/api/llm-credentials', () => ({
  listLlmCredentials: vi.fn(async () => [
    {
      id: 'cred-1',
      name: 'My OpenAI key',
      description: 'prod',
      provider: 'OpenAI',
      model_context_tokens: 128000,
      created_at: '2026-06-01T00:00:00Z',
      updated_at: '2026-06-02T00:00:00Z',
      access: { capabilities: ['manage_secret', 'delete'] },
    },
  ]),
  getLlmCredential: vi.fn(),
  createLlmCredential: vi.fn(),
  updateLlmCredential: vi.fn(),
  deleteLlmCredential: vi.fn(),
  testLlmCredentialConnection: testConnectionMock,
  getOpenRouterConnection: openRouterStatusMock,
  startOpenRouterConnection: vi.fn(),
  completeOpenRouterConnection: completeOpenRouterMock,
  refreshOpenRouterModels: refreshOpenRouterMock,
  disconnectOpenRouter: disconnectOpenRouterMock,
}));

// sonner toast is a side effect we don't assert on here.
vi.mock('sonner', () => ({
  toast: { success: toastSuccessMock, error: toastErrorMock },
}));

import { CredentialsSettingsPanel } from '@/pages/credentials/CredentialsPage';

void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: {
    en: { translation: en },
    zh: { translation: zh },
  },
  interpolation: { escapeValue: false },
});

function LocationProbe() {
  const location = useLocation();
  return (
    <output data-testid="location-probe">
      {location.pathname}{location.search}
    </output>
  );
}

function renderPage(initialEntry = '/settings?tab=api-keys') {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <I18nextProvider i18n={i18n}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[initialEntry]}>
          <Routes>
            <Route path="/settings" element={<CredentialsSettingsPanel />} />
            <Route
              path="/settings/openrouter/callback/:openrouterState"
              element={<CredentialsSettingsPanel />}
            />
          </Routes>
          <LocationProbe />
        </MemoryRouter>
      </QueryClientProvider>
    </I18nextProvider>,
  );
}

describe('CredentialsSettingsPanel', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('en');
    openRouterStatusMock.mockResolvedValue({
      connected: false,
      credential_id: null,
      models: [],
      catalog_refreshed_at: null,
      catalog_stale: false,
      error_code: null,
    });
    refreshOpenRouterMock.mockReset();
    disconnectOpenRouterMock.mockReset();
    disconnectOpenRouterMock.mockResolvedValue(undefined);
    completeOpenRouterMock.mockReset();
    toastErrorMock.mockReset();
    toastSuccessMock.mockReset();
  });

  it('shows dynamic OpenRouter model metadata and refresh controls without advertising Codex', async () => {
    openRouterStatusMock.mockResolvedValue({
      connected: true,
      credential_id: 'openrouter-1',
      models: [{
        id: 'openai/gpt-5',
        name: 'GPT-5',
        description: 'Primary',
        context_length: 400000,
        input_modalities: ['text', 'image'],
        output_modalities: ['text'],
        supports_tools: true,
        supported_reasoning_efforts: ['low', 'medium', 'high'],
        default_reasoning_effort: 'medium',
        pricing: { prompt: '0.000001', completion: '0.000004' },
        available: true,
      }],
      catalog_refreshed_at: '2026-08-23T00:00:00Z',
      catalog_stale: false,
      error_code: null,
    });
    refreshOpenRouterMock.mockResolvedValue(await openRouterStatusMock());
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText('1 models available in Chat')).toBeInTheDocument();
    expect(screen.getByText(/0 free models/)).toBeInTheDocument();
    expect(screen.getByText(/including Codex through OpenRouter Responses API/)).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Default Chat model' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Refresh models' }));
    await waitFor(() => expect(refreshOpenRouterMock).toHaveBeenCalledOnce());
  });

  it('handles an empty catalog and requires an explicit second click to disconnect', async () => {
    openRouterStatusMock.mockResolvedValue({
      connected: true,
      credential_id: 'openrouter-empty',
      models: [],
      catalog_refreshed_at: '2026-08-23T00:00:00Z',
      catalog_stale: false,
      error_code: null,
    });
    refreshOpenRouterMock.mockResolvedValue(await openRouterStatusMock());
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText(/No compatible text and tool-capable models/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reconnect OpenRouter' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Disconnect' }));
    expect(disconnectOpenRouterMock).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Confirm disconnect' }));
    await waitFor(() => expect(disconnectOpenRouterMock).toHaveBeenCalledOnce());
  });

  it('shows revoked and transient catalog failures without discarding the selected model', async () => {
    openRouterStatusMock.mockResolvedValue({
      connected: false,
      credential_id: 'openrouter-revoked',
      models: [{
        id: 'openai/gpt-5',
        name: 'GPT-5',
        description: null,
        context_length: 400000,
        input_modalities: ['text'],
        output_modalities: ['text'],
        supports_tools: true,
        supported_reasoning_efforts: [],
        default_reasoning_effort: null,
        pricing: { prompt: null, completion: null },
        available: false,
      }],
      catalog_refreshed_at: '2026-08-23T00:00:00Z',
      catalog_stale: true,
      error_code: 'openrouter_credentials_rejected',
    });
    const { unmount } = renderPage();

    expect(await screen.findByText(/OpenRouter rejected this key/)).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Default Chat model' })).not.toBeInTheDocument();
    unmount();

    openRouterStatusMock.mockResolvedValue({
      connected: true,
      credential_id: 'openrouter-transient',
      models: [{
        id: 'openai/gpt-5',
        name: 'GPT-5',
        description: null,
        context_length: 400000,
        input_modalities: ['text'],
        output_modalities: ['text'],
        supports_tools: true,
        supported_reasoning_efforts: [],
        default_reasoning_effort: null,
        pricing: { prompt: null, completion: null },
        available: true,
      }],
      catalog_refreshed_at: '2026-08-23T00:00:00Z',
      catalog_stale: true,
      error_code: 'openrouter_catalog_unavailable',
    });
    renderPage();
    expect(await screen.findByText(/previous catalog has been kept/)).toBeInTheDocument();
  });

  it('sanitizes an incomplete OAuth callback and removes callback parameters', async () => {
    renderPage('/settings/openrouter/callback/fixture-state?error=access_denied&error_description=private');

    await waitFor(() => expect(toastErrorMock).toHaveBeenCalledWith(
      'OpenRouter did not complete the connection. Start again.',
    ));
    await waitFor(() => expect(screen.getByTestId('location-probe')).toHaveTextContent(
      '/settings?tab=api-keys',
    ));
    expect(document.body).not.toHaveTextContent('private');
  });

  it('completes a callback using state from the stable callback path', async () => {
    completeOpenRouterMock.mockResolvedValue({
      connected: true,
      credential_id: 'openrouter-connected',
      models: [],
      catalog_refreshed_at: '2026-08-23T00:00:00Z',
      catalog_stale: false,
      error_code: null,
    });
    renderPage('/settings/openrouter/callback/fixture-state?code=single-use-code');

    await waitFor(() => expect(completeOpenRouterMock).toHaveBeenCalledWith(
      'single-use-code',
      'fixture-state',
    ));
    await waitFor(() => expect(screen.getByTestId('location-probe')).toHaveTextContent(
      '/settings?tab=api-keys',
    ));
    expect(toastSuccessMock).toHaveBeenCalledWith('OpenRouter connected');
  });

  it('explains a deployment network failure instead of showing a generic callback error', async () => {
    completeOpenRouterMock.mockRejectedValue(Object.assign(
      new Error('completeOpenRouterConnection failed: 502'),
      { code: 'openrouter_unreachable' },
    ));
    renderPage('/settings/openrouter/callback/fixture-state?code=single-use-code');

    await waitFor(() => expect(toastErrorMock).toHaveBeenCalledWith(
      'Flowork could not reach OpenRouter. Check the deployment network or proxy, then start again.',
    ));
    await waitFor(() => expect(screen.getByTestId('location-probe')).toHaveTextContent(
      '/settings?tab=api-keys',
    ));
  });

  it('keeps the section heading and primary action in one responsive header', async () => {
    renderPage();
    const header = screen.getByTestId('credentials-section-header');
    expect(header).toHaveClass('sm:flex-row', 'sm:items-start', 'sm:justify-between');
    expect(within(header).getByRole('heading', { name: 'API credentials' })).toBeInTheDocument();
    expect(within(header).getByRole('button', { name: 'Add key' })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: 'Connect OpenRouter' })).toBeInTheDocument();
    expect(screen.getByText(/does not sign you in to Flowork/i)).toBeInTheDocument();
  });

  it('renders a permanently masked write-only credential row', async () => {
    renderPage();
    await waitFor(() =>
      expect(screen.getByText('My OpenAI key')).toBeInTheDocument(),
    );
    // Public fields present.
    expect(screen.getByText('OpenAI')).toBeInTheDocument();
    // Secret material is neither returned nor exposed through a UI control.
    expect(screen.getByText('••••••••')).toBeInTheDocument();
    expect(screen.getByText('Stored · write-only')).toBeInTheDocument();
    expect(screen.getByRole('table')).toHaveClass('min-w-[760px]');
    expect(screen.getByTestId('credentials-table-scroll')).toHaveClass('overflow-x-auto');
    expect(screen.queryByRole('button', { name: 'Reveal key' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Copy key' })).not.toBeInTheDocument();
  });

  it('tests the stored key server-side and renders only a sanitized result', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Test connection' }));
    await waitFor(() => expect(testConnectionMock).toHaveBeenCalledWith('cred-1'));
    expect(screen.getByText(/Connected/)).toHaveTextContent('Connected · 42 ms');
    expect(screen.queryByText(/fixture-key/i)).not.toBeInTheDocument();
  });

  it('uses native Simplified Chinese copy throughout the list and connection state', async () => {
    await i18n.changeLanguage('zh');
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole('heading', { name: 'API 凭据' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '添加密钥' })).toBeInTheDocument();
    expect(await screen.findByText('My OpenAI key')).toBeInTheDocument();
    expect(screen.getByText('提供商')).toBeInTheDocument();
    expect(screen.getByText('上下文长度')).toBeInTheDocument();
    expect(screen.getByText('已保存 · 只写')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '连接 OpenRouter' })).toBeInTheDocument();
    expect(screen.getByText(/不会用于登录 Flowork/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '测试连接' }));
    await waitFor(() => expect(screen.getByText(/连接成功/)).toHaveTextContent('连接成功 · 42 ms'));
    expect(screen.queryByText(/Stored|Connected|Test connection/)).not.toBeInTheDocument();
  });

  it('keeps the add-credential dialog fully localized in Simplified Chinese', async () => {
    await i18n.changeLanguage('zh');
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole('button', { name: '添加密钥' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('heading', { name: '添加凭据' })).toBeInTheDocument();
    expect(within(dialog).getByText('基本信息')).toBeInTheDocument();
    expect(within(dialog).getByText('提供商与模型')).toBeInTheDocument();
    expect(within(dialog).getByText('连接设置')).toBeInTheDocument();
    expect(within(dialog).getByRole('heading', { name: '密钥' })).toBeInTheDocument();
    expect(within(dialog).getByLabelText('名称')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('提供商')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('模型')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('模型上下文长度')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('API 密钥')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: '保存' })).toBeDisabled();
    expect(within(dialog).queryByText(/Identity|Provider and model|Connection|Secret/)).not.toBeInTheDocument();
  });
});
