/**
 * Deployments T14 — `CreateDeploymentModal` one-shot-secret test.
 *
 * Asserts the modal switches from form → success panel after a
 * successful `createDeployment`, and that the one-shot `api_key`
 * is visible in the DOM (this is the only place the user will ever
 * reveal or copy the plaintext secret).
 *
 * Two render targets matter:
 *   * `data-testid="one-shot-secret"` — the password input carrying
 *     the credential. Stable test selector so a styling
 *     refactor doesn't break the assertion.
 *   * Password masking by default, followed by explicit reveal.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import i18n from 'i18next';

vi.mock('@/lib/api/deployments', () => ({
  createDeployment: vi.fn(async () => ({
    id: 'dep-1',
    api_key: 'vc_test_plaintext_key_abc123',
    endpoint_url: '/api/v1/deployments/test-bot/invoke',
  })),
}));

const workflowQueryState = vi.hoisted(() => ({
  items: [] as Array<{
    wf_id: string;
    workflow_name: string;
    description: string;
  }>,
}));

vi.mock('@/lib/api/queries/workflows', () => ({
  useWorkspaceList: () => ({
    data: { items: workflowQueryState.items },
    isLoading: false,
  }),
}));

import { createDeployment } from '@/lib/api/deployments';
vi.mock('@/lib/api/queries/workflow', () => ({
  useWorkflowVersions: () => ({ data: { versions: [{ major: 1, sub: 2 }] }, isLoading: false }),
}));
import { CreateDeploymentModal } from '@/pages/deployments/CreateDeploymentModal';

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: {} } },
  interpolation: { escapeValue: false },
});

function renderModal(initialWorkflowId = 'wf_42', options: Partial<React.ComponentProps<typeof CreateDeploymentModal>> = {}) {
  const onOpenChange = vi.fn();
  const onCreated = vi.fn();
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0 },
      mutations: { retry: false },
    },
  });
  const utils = render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={testI18n}>
        <MemoryRouter>
          <CreateDeploymentModal
            open
            onOpenChange={onOpenChange}
            onCreated={onCreated}
            initialWorkflowId={initialWorkflowId}
            {...options}
          />
        </MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  );
  return { ...utils, onOpenChange, onCreated };
}

describe('<CreateDeploymentModal>', () => {
  beforeEach(() => {
    workflowQueryState.items = [];
    vi.clearAllMocks();
    Element.prototype.scrollIntoView = vi.fn();
  });

  it('renders the one-shot api_key after a successful create', async () => {
    const user = userEvent.setup();
    renderModal();

    // Fill the form. Modal opens with `trigger_type=api` and
    // `version_pin=head` defaults, so we only need the three
    // required name field. Workflow is supplied by the launch context and the
    // endpoint slug is derived automatically from the deployment name.
    await user.type(screen.getByLabelText(/^Name$/), 'API bot');
    expect(screen.queryByLabelText(/^Slug$/)).toBeNull();
    fireEvent.submit(screen.getByRole('button', { name: /create/i }).closest('form')!);

    // Wait for the success panel.
    await waitFor(() => {
      expect(createDeployment).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(
        screen.getByText(/Deployment created/i),
      ).toBeInTheDocument();
    });

    // The one-time value is present for copy/reveal, but masked by default.
    const secret = await screen.findByTestId('one-shot-secret') as HTMLInputElement;
    expect(secret).toHaveAttribute('type', 'password');
    expect(secret).toHaveValue('vc_test_plaintext_key_abc123');
    await user.click(screen.getByRole('button', { name: /show secret/i }));
    expect(secret).toHaveAttribute('type', 'text');

    // And the "this is shown only once" warning is surfaced.
    expect(
      screen.getByText(/shown only once/i),
    ).toBeInTheDocument();

    // The API was called with the form body — basic shape only;
    // we don't snapshot the full payload because the modal strips
    // empty fields conditionally.
    expect(createDeployment).toHaveBeenCalledTimes(1);
    const callArg = vi.mocked(createDeployment).mock.calls[0][0];
    expect(callArg.name).toBe('API bot');
    expect(callArg.slug).toBe('api-bot');
    expect(callArg.wf_id).toBe('wf_42');
    expect(callArg.trigger_type).toBe('api');
    expect(callArg.version_pin).toBe('specific');
    expect(callArg.pinned_sub).toBe(2);
    expect(callArg.pinned_major).toBe(1);
    expect(callArg.mount_enabled).toBe(false);
    expect(callArg.enabled).toBe(true);
  });

  it('lets users scroll and select a workflow from the modal dropdown', async () => {
    workflowQueryState.items = Array.from({ length: 30 }, (_, index) => ({
      wf_id: `wf_${String(index + 1).padStart(2, '0')}`,
      workflow_name: `Workflow ${String(index + 1).padStart(2, '0')}`,
      description: `Description ${index + 1}`,
    }));
    const user = userEvent.setup();
    renderModal('');

    const trigger = screen.getByLabelText('Workflow');
    await user.click(trigger);

    const listbox = screen.getByRole('listbox');
    expect(listbox).toHaveClass('overflow-y-auto');
    expect(listbox.parentElement).toHaveClass('pointer-events-auto');

    await user.click(screen.getByRole('option', { name: /Workflow 24/ }));

    expect(trigger).toHaveTextContent('Workflow 24');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

});

describe('inline workflow deployment', () => {
  beforeEach(() => { vi.clearAllMocks(); });
  it('pins the version returned by saving, keeps the secret visible, and refreshes the related list', async () => {
    const prepare = vi.fn().mockResolvedValue('v2.sv4');
    const { onCreated } = renderModal('wf_42', { inline: true, initialName: 'Saved version', context: { version: 'v2.sv3', dirty: true, prepare } });
    fireEvent.submit(screen.getByRole('button', { name: 'Save and deploy' }).closest('form')!);
    await waitFor(() => expect(createDeployment).toHaveBeenCalledWith(expect.objectContaining({ wf_id: 'wf_42', version_pin: 'specific', pinned_major: 2, pinned_sub: 4 })));
    expect(prepare).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId('one-shot-secret')).toBeVisible();
    expect(screen.getByRole('link', { name: 'Details ↗' })).toHaveAttribute('href', '/deployments/dep-1');
  });
  it('does not create a deployment if saving fails', async () => {
    const prepare = vi.fn().mockRejectedValue(new Error('version conflict'));
    renderModal('wf_42', { inline: true, initialName: 'Conflict', context: { version: 'v1.sv1', dirty: true, prepare } });
    fireEvent.submit(screen.getByRole('button', { name: 'Save and deploy' }).closest('form')!);
    await waitFor(() => expect(prepare).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save and deploy' })).not.toBeDisabled());
    expect(createDeployment).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Name')).toHaveValue('Conflict');
  });
});
