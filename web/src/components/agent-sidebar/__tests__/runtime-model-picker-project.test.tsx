import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';

import { RuntimeModelPicker } from '../RuntimeModelPicker';
import type { AgentRuntimeCapabilities } from '@/lib/api/agent-runtime';

afterEach(cleanup);

it('explains Project connection ownership without a retired Chat binding discriminator', async () => {
  const capabilities: AgentRuntimeCapabilities = {
    protocol_version: 2,
    runtime_type: 'codex',
    runtime_available: true,
    authenticated: true,
    source: 'test',
    models: [{
      id: 'codex:account:test-model', label: 'Test model', description: '',
      provider: null, is_default: true, supported_reasoning_efforts: [],
      default_reasoning_effort: null,
    }],
    default_model_id: 'codex:account:test-model',
    error_code: null,
    bound_agent_settings: {
      model_id: 'codex:account:test-model', temperature: null, max_tokens: null,
      timeout: null, reasoning_effort: null,
    },
  };
  render(<RuntimeModelPicker capabilities={capabilities} loading={false} onChange={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: /^Model$/ }));
  expect(screen.getByText('All chats in this project share this connection. Create another project to use a different source.')).toBeInTheDocument();
  expect(screen.queryByText(/Start a new Chat to use another source/)).not.toBeInTheDocument();
});
