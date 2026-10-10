import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import '@/lib/i18n';
import i18n from '@/lib/i18n';
import { InteractiveArtifactBlock, InteractiveArtifactPreview } from './InteractiveArtifactBlock';
import type { InteractiveArtifact } from './interactive-artifact-contract';

const artifact: InteractiveArtifact = {
  kind: 'interactive_artifact', artifact_id: 'original-artifact', hitl_request_id: 'original-approval',
  component_type: 'approval', completion_mode: 'wait_for_submit', read_only: true,
  props: { fields: [{ name: 'tool', value: 'original action' }] },
  interaction_state: { status: 'pending' },
};

describe('branch interaction snapshots', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('en');
    vi.stubGlobal('fetch', vi.fn());
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it.each(['inline', 'preview'])('does not hydrate or offer approval in the %s surface', (surface) => {
    const submit = vi.fn();
    render(surface === 'preview'
      ? <InteractiveArtifactPreview artifact={artifact} onSubmitAsNewMessage={submit} />
      : <InteractiveArtifactBlock showAvatar={false} call={{ id: 'tool', name: 'approval', arguments: '{}', status: 'done',
        artifact: { payload: { artifact, hitl_type: 'pre_tool_approval' } },
      }} onSubmitAsNewMessage={submit} />);
    expect(screen.getByText('Historical interaction (read-only)')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^(Approve|Deny|Submit|Cancel)$/ })).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
    expect(submit).not.toHaveBeenCalled();
  });
});
