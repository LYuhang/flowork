import { describe, expect, it } from 'vitest';
import { interactiveArtifactRenderError } from './interactive-artifact-contract';

describe('interactive artifact renderer contract', () => {
  it('accepts pinned workflow references but rejects invalid versions and inline graphs', () => {
    const artifact = { kind: 'interactive_artifact', component_type: 'workflow_preview', props: { workflow_id: 'wf', version: 'v2.sv3' } } as const;
    expect(interactiveArtifactRenderError(artifact)).toBeNull();
    expect(interactiveArtifactRenderError({ ...artifact, props: { workflow_id: 'wf', version: 'latest' } })).toContain('/version');
    expect(interactiveArtifactRenderError({ ...artifact, props: { ...artifact.props, workflow: {} } })).toContain('/workflow');
  });
  it('accepts the generated HTML and file preview variants', () => {
    expect(interactiveArtifactRenderError({
      kind: 'interactive_artifact',
      component_type: 'html_preview',
      props: { html: '<main>safe preview</main>' },
    })).toBeNull();
    expect(interactiveArtifactRenderError({
      kind: 'interactive_artifact',
      component_type: 'file_preview',
      props: { path: '/mount/report.pdf', file_type: 'pdf' },
    })).toBeNull();
  });

  it('fails closed for unsupported, incomplete, and extra renderer fields', () => {
    expect(interactiveArtifactRenderError({
      kind: 'interactive_artifact',
      component_type: 'html_preview',
      props: { html: '' },
    })).toContain('/html');
    expect(interactiveArtifactRenderError({
      kind: 'interactive_artifact',
      component_type: 'file_preview',
      props: { path: '/mount/report.pdf', unexpected: true },
    })).toContain('/unexpected');
    expect(interactiveArtifactRenderError({
      kind: 'interactive_artifact',
      component_type: 'approval',
      props: {},
    })).toContain('approval fields are missing');
  });
});
