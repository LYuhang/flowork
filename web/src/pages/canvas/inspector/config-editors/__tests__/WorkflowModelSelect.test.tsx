import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { WorkflowModelSelect } from '../WorkflowModelSelect';

const state = vi.hoisted(() => ({
  data: { models: {} as Record<string, { provider: string }> },
  isPending: false, isError: false, refetch: vi.fn(),
}));
vi.mock('@/lib/api/queries/llm-credentials', () => ({ useWorkflowModels: () => state }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_: string, fallback: string) => fallback }) }));

beforeEach(() => {
  state.data = { models: { 'manual-openrouter': { provider: 'openrouter' } } };
  state.isPending = false;
  state.isError = false;
  state.refetch.mockClear();
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => {};
  Element.prototype.releasePointerCapture = () => {};
  Element.prototype.scrollIntoView = () => {};
});

describe('Workflow manual API picker', () => {
  it('allows a manually added OpenRouter API, not arbitrary input', async () => {
    const onChange = vi.fn();
    render(<WorkflowModelSelect value="" testId="picker" onChange={onChange} />);
    const user = userEvent.setup();
    await user.click(screen.getByTestId('picker'));
    await user.click(screen.getByText('manual-openrouter (openrouter)'));
    expect(onChange).toHaveBeenCalledWith('manual-openrouter');
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('preserves an old model visibly but makes it unselectable', async () => {
    const onChange = vi.fn();
    render(<WorkflowModelSelect value="Default API" testId="picker" onChange={onChange} />);
    expect(screen.getByRole('status')).toHaveTextContent('saved value has not been changed');
    await userEvent.setup().click(screen.getByTestId('picker'));
    expect(screen.getByRole('option', { name: 'Default API (unavailable)' })).toHaveAttribute('aria-disabled', 'true');
    expect(onChange).not.toHaveBeenCalled();
  });

  it('does not permit stale cached choices after a failed refresh', () => {
    state.isError = true;
    render(<WorkflowModelSelect value="manual-openrouter" testId="picker" onChange={vi.fn()} />);
    expect(screen.getByTestId('picker')).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(state.refetch).toHaveBeenCalledOnce();
  });

  it('distinguishes loading, empty and read-only states', () => {
    state.isPending = true;
    state.data = { models: {} };
    const { rerender } = render(<WorkflowModelSelect value="" testId="picker" onChange={vi.fn()} />);
    expect(screen.getByTestId('picker')).toBeDisabled();
    expect(screen.getByText('Loading model APIs…')).toBeInTheDocument();
    state.isPending = false;
    rerender(<WorkflowModelSelect value="" testId="picker" onChange={vi.fn()} />);
    expect(screen.getByRole('status')).toHaveTextContent('Add a model API');
    state.data = { models: { 'manual-openrouter': { provider: 'openrouter' } } };
    rerender(<WorkflowModelSelect value="manual-openrouter" readOnly testId="picker" onChange={vi.fn()} />);
    expect(screen.getByTestId('picker')).toBeDisabled();
  });
});
