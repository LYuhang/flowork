import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@/lib/i18n';
import i18n from '@/lib/i18n';
import { ChoicesCard } from './ChoicesCard';
import type { InteractiveArtifact } from './interactive-artifact-contract';

const artifact: InteractiveArtifact = {
  kind: 'interactive_artifact', artifact_id: 'choice-test', hitl_request_id: 'hitl-test',
  component_type: 'user_input', title: 'Choose a file',
  props: { mode: 'choices', questions: [{ multiple: false, options: [
    { value: 'a', label: 'Report A', description: 'First file' }, { value: 'b', label: 'Report B' },
  ] }] },
};

describe('ChoicesCard', () => {
  let status = 'pending';
  let selected: string[] = [];
  let posts = 0;
  beforeEach(async () => {
    await i18n.changeLanguage('en');
    sessionStorage.clear();
    status = 'pending'; selected = []; posts = 0;
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        posts++;
        const body = JSON.parse(String(init.body));
        status = body.decision === 'submit' ? 'submitted' : 'cancelled';
        selected = status === 'submitted' ? body.decision_payload.widget_state.selected_ids : [];
      }
      return new Response(JSON.stringify({ status, interaction_result_json: { selected_ids: selected } }), { status: 200 });
    }));
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('requires selection, submits once, freezes the selected labels', async () => {
    render(<ChoicesCard artifact={artifact} />);
    const confirm = screen.getByRole('button', { name: 'Confirm selection' });
    expect(confirm).toBeDisabled();
    fireEvent.click(screen.getByRole('radio', { name: 'Report B' }));
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    await screen.findByText('Selection confirmed');
    expect(posts).toBe(1);
    expect(screen.getByRole('radio', { name: 'Report B' })).toBeChecked();
    expect(screen.getByRole('radio', { name: 'Report B' })).toBeDisabled();
  });

  it('cancels without requiring any selection', async () => {
    render(<ChoicesCard artifact={artifact} />);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await screen.findByText('Selection cancelled');
    expect(selected).toEqual([]);
  });

  it('preserves an unsubmitted draft across remount, without confirming it', async () => {
    const first = render(<ChoicesCard artifact={artifact} />);
    fireEvent.click(screen.getByRole('radio', { name: 'Report B' }));
    first.unmount();
    render(<ChoicesCard artifact={artifact} />);
    expect(screen.getByRole('radio', { name: 'Report B' })).toBeChecked();
    await waitFor(() => expect(posts).toBe(0));
  });

  it('reconciles a decision made by another tab instead of overriding it', async () => {
    render(<ChoicesCard artifact={artifact} />);
    fireEvent.click(screen.getByRole('radio', { name: 'Report B' }));
    status = 'submitted'; selected = ['a'];
    fireEvent.click(screen.getByRole('button', { name: 'Confirm selection' }));
    await screen.findByText('Selection confirmed');
    expect(posts).toBe(0);
    expect(screen.getByRole('radio', { name: /Report A/ })).toBeChecked();
  });
});
