import { describe, expect, it } from 'vitest';
import { groupToolActivity } from '@/components/agent-sidebar/chat-render-groups';
import type { MergedMessage } from '@/components/agent-sidebar/types';

function tool(id: string, name: string) {
  return { id, name, arguments: '{}', status: 'done' as const, result: 'ok' };
}

function interactiveTool(id: string) {
  return {
    ...tool(id, 'render_interactive'),
    artifact: {
      status: 'success',
      payload: {
        kind: 'interactive_artifact',
        artifact: {
          kind: 'interactive_artifact',
          artifact_id: `ia_${id}`,
          title: 'Choose a value',
          component_type: 'slider',
          props: { label: 'Count', min: 1, max: 10, step: 1, value: 3 },
          completion_mode: 'wait_for_submit',
        },
      },
    },
  };
}

describe('groupToolActivity', () => {
  it.each(['render_preview', 'render_interactive', 'render_url_preview'])(
    'keeps %s as a standalone card after backend success', (name) => {
      const items = groupToolActivity([{
        role: 'assistant', content: '', tool_calls: [{
          ...tool('preview', name), artifact: { status: 'success' },
        }],
      }]);
      expect(items.map((item) => item.kind)).toEqual(['interactive_artifact']);
    },
  );
  it('starts a new tool block when a later assistant text message appears', () => {
    const messages: MergedMessage[] = [
      { role: 'assistant', content: 'first text', tool_calls: [tool('a', 'write_file')] },
      { role: 'assistant', content: '', tool_calls: [tool('b', 'todo')] },
      { role: 'assistant', content: 'second text', tool_calls: [tool('c', 'update_canvas')] },
    ];

    const items = groupToolActivity(messages);

    expect(items.map((item) => item.kind)).toEqual([
      'message',
      'tool_group',
      'message',
      'tool_group',
    ]);
    expect(items[1]).toMatchObject({
      kind: 'tool_group',
      calls: [{ id: 'a' }, { id: 'b' }],
    });
    expect(items[3]).toMatchObject({
      kind: 'tool_group',
      calls: [{ id: 'c' }],
    });
  });

  it('renders render_interactive as a standalone item and does not merge surrounding tools', () => {
    const messages: MergedMessage[] = [
      {
        role: 'assistant',
        content: '',
        tool_calls: [
          tool('a', 'browser_snapshot'),
          interactiveTool('b'),
          tool('c', 'browser_click'),
        ],
      },
    ];

    const items = groupToolActivity(messages);

    expect(items.map((item) => item.kind)).toEqual([
      'tool_group',
      'interactive_artifact',
      'tool_group',
    ]);
    expect(items[0]).toMatchObject({
      kind: 'tool_group',
      calls: [{ id: 'a' }],
    });
    expect(items[1]).toMatchObject({
      kind: 'interactive_artifact',
      call: { id: 'b' },
    });
    expect(items[2]).toMatchObject({
      kind: 'tool_group',
      calls: [{ id: 'c' }],
    });
  });

  it('renders a successful interactive artifact even before a separate tool status frame arrives', () => {
    const interactive = {
      ...interactiveTool('streaming'),
      status: 'running' as const,
    };
    const messages: MergedMessage[] = [{
      role: 'assistant',
      content: '',
      tool_calls: [interactive],
    }];

    expect(groupToolActivity(messages)).toMatchObject([{
      kind: 'interactive_artifact',
      call: { id: 'streaming', status: 'running' },
    }]);
  });

  it('keeps a backend-successful but malformed artifact standalone for an explicit render fallback', () => {
    const malformed = {
      ...tool('b', 'render_interactive'),
      artifact: {
        status: 'success',
        payload: {
          kind: 'interactive_artifact',
          artifact: {
            kind: 'interactive_artifact',
            artifact_id: 'ia_broken',
            component_type: 'slider',
            props: { min: 1, max: 10 },
          },
        },
      },
    };
    const messages: MergedMessage[] = [{
      role: 'assistant',
      content: '',
      tool_calls: [tool('a', 'browser_snapshot'), malformed, tool('c', 'browser_click')],
    }];

    const items = groupToolActivity(messages);

    expect(items.map((item) => item.kind)).toEqual([
      'tool_group',
      'interactive_artifact',
      'tool_group',
    ]);
    expect(items[1]).toMatchObject({ kind: 'interactive_artifact', call: { id: 'b' } });
  });

  it('keeps render_interactive tool errors inside the normal tool block', () => {
    const failed = {
      ...tool('b', 'render_interactive'),
      status: 'error' as const,
      artifact: {
        status: 'error',
        artifact: { kind: 'tool_error' },
      },
    };
    const messages: MergedMessage[] = [{
      role: 'assistant',
      content: '',
      tool_calls: [failed],
    }];

    expect(groupToolActivity(messages)).toMatchObject([{
      kind: 'tool_group',
      calls: [{ id: 'b', status: 'error' }],
    }]);
  });

  it('renders a running pre-tool approval as a standalone interactive card', () => {
    const approval = {
      id: 'approval-1',
      name: 'shell',
      arguments: '{"command":"touch /data/example"}',
      status: 'running' as const,
      artifact: {
        status: 'success',
        meta: {
          hitl_type: 'pre_tool_approval',
          pending_approval: true,
        },
        payload: {
          kind: 'interactive_artifact',
          pending_approval: true,
          hitl_type: 'pre_tool_approval',
          artifact: {
            kind: 'interactive_artifact',
            artifact_id: 'ia_approval_1',
            hitl_request_id: 'hitl_approval_1',
            component_type: 'approval',
            completion_mode: 'wait_for_submit',
          },
        },
      },
    };
    const messages: MergedMessage[] = [{
      role: 'assistant',
      content: '',
      tool_calls: [tool('a', 'read_file'), approval, tool('c', 'write_file')],
    }];

    expect(groupToolActivity(messages).map((item) => item.kind)).toEqual([
      'tool_group',
      'interactive_artifact',
      'tool_group',
    ]);
  });
});
