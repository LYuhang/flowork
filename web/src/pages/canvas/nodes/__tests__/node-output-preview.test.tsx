import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';
import { server } from '@/__tests__/msw-handlers';
import { NodeOutputPreview } from '../NodeOutputPreview';
import { NODE_LABELS } from '../NODE_TYPES';
import { useExecStreamStore } from '@/stores/exec-stream';
import i18n from '@/lib/i18n';

beforeEach(async () => {
  await i18n.changeLanguage('en');
  useExecStreamStore.getState().reset();
});

function show(output?: unknown, nodeType = 'TemplateNode', status = 'completed') {
  let reads = 0;
  server.use(http.get('*/api/v1/vfs/content', ({ request }) => {
    const url = new URL(request.url);
    expect(url.searchParams.get('run_id')).toBe('wf-preview');
    expect(url.searchParams.get('path')).toBe('/run/__exec__/nodes/node_3.json');
    reads += 1;
    return output === undefined
      ? HttpResponse.json({ detail: 'missing' }, { status: 404 })
      : HttpResponse.json({ content: JSON.stringify({ status, output }) });
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/workflow/wf-preview']}>
        <Routes><Route path="/workflow/:wfId" element={<NodeOutputPreview nodeId="node_3" nodeType={nodeType} />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, reads: () => reads };
}

describe('Template canvas output', () => {
  it('occupies no space before the node has output', async () => {
    const view = show();
    await waitFor(() => expect(view.reads()).toBe(1));
    expect(view.container).toBeEmptyDOMElement();
  });

  it('hydrates persisted HTML, keeps scripts disabled, and supports collapse/expand', async () => {
    const view = show({ rendered: '<h3>Preview g j p y</h3>', format: 'html' });
    const iframe = await screen.findByTestId('template-preview-iframe');
    expect(iframe).toHaveAttribute('sandbox', '');
    expect(iframe.getAttribute('srcdoc')).toContain('Preview g j p y');
    const button = screen.getByRole('button', { name: 'Output preview' });
    expect(button).toHaveAttribute('aria-expanded', 'true');
    expect(document.getElementById(button.getAttribute('aria-controls')!)).toHaveClass('max-h-60', 'overflow-auto');
    expect(button.parentElement).toHaveClass('w-56');
    fireEvent.click(button);
    expect(button).toHaveAttribute('aria-expanded', 'false');
    expect(button.parentElement).toHaveClass('w-56');
    expect(screen.queryByTestId('rendered-preview')).toBeNull();
    fireEvent.click(button);
    expect(await screen.findByTestId('template-preview-iframe')).toBeInTheDocument();
    expect(view.reads()).toBe(1);
  });

  it('does not mix an unrelated workflow’s output into this node', async () => {
    useExecStreamStore.setState({ wfId: 'other', status: 'completed', perNode: {
      node_3: { status: 'completed', result: JSON.stringify({ rendered: 'Wrong workflow', format: 'text' }) },
    } });
    show({ rendered: 'Saved output', format: 'text' });
    expect(await screen.findByText('Saved output')).toBeInTheDocument();
    expect(screen.queryByText('Wrong workflow')).toBeNull();
  });

  it('hides old output on rerun and displays this node’s new completed output', async () => {
    show({ rendered: 'Old result', format: 'text' });
    expect(await screen.findByText('Old result')).toBeInTheDocument();
    act(() => useExecStreamStore.getState().begin('wf-preview', new AbortController()));
    expect(screen.queryByText('Old result')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Output preview' })).toBeNull();
    act(() => useExecStreamStore.setState({ perNode: {
      node_3: { status: 'completed', result: JSON.stringify({ rendered: '**New result**', format: 'markdown' }) },
    } }));
    expect(await screen.findByText('New result')).toBeInTheDocument();
    expect(screen.queryByText('Old result')).toBeNull();
  });

  it('does not present an errored node’s stale result as successful output', async () => {
    useExecStreamStore.setState({ wfId: 'wf-preview', status: 'error', perNode: {
      node_3: { status: 'error', result: JSON.stringify({ rendered: 'Stale', format: 'text' }) },
    } });
    const view = show({ rendered: 'Old result', format: 'text' });
    expect(view.container).toBeEmptyDOMElement();
    expect(view.reads()).toBe(0);
  });
});

describe('Shared canvas output', () => {
  it.each(Object.keys(NODE_LABELS))('shows persisted output for %s', async (nodeType) => {
    show({ message: 'Result for every node', count: 0 }, nodeType);
    expect(await screen.findByText('"Result for every node"')).toBeInTheDocument();
    const button = screen.getByRole('button', { name: 'Output preview' });
    expect(button.parentElement).toHaveClass('w-56');
    expect(button.parentElement).toHaveAttribute('data-node-output-preview', 'node_3');
  });

  it.each([
    [null, 'null'], [false, 'false'], [0, '0'], ['', '""'], [{}, '{}'], [[], '[]'],
  ])('does not hide a valid empty/primitive output: %j', async (output, expected) => {
    show(output, 'CodeNode');
    expect(await screen.findByText(expected as string)).toBeInTheDocument();
  });

  it('keeps arbitrary HTML-looking output inert, even with rendered/format fields', async () => {
    show({ rendered: '<img src=x onerror=alert(1)>', format: 'html' }, 'CodeNode');
    expect(await screen.findByText('"<img src=x onerror=alert(1)>"')).toBeInTheDocument();
    expect(document.querySelector('iframe, img')).toBeNull();
  });

  it('bounds arrays and lazily opens nested values', async () => {
    show(Array.from({ length: 10000 }, (_, index) => ({ value: `item-${index}` })), 'TableReadNode');
    const more = await screen.findByRole('button', { name: 'Show more (9980)' });
    expect(document.querySelectorAll('[data-node-json-preview] button')).toHaveLength(22);
    expect(screen.queryByText('"item-0"')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /"0":\s*\{1\}/ }));
    expect(screen.getByText('"item-0"')).toBeInTheDocument();
    fireEvent.click(more);
    expect(screen.getByRole('button', { name: 'Show more (9960)' })).toBeInTheDocument();
  });

  it('bounds long strings and reveals more on demand', async () => {
    show('x'.repeat(700) + 'THE_END', 'PromptNode');
    const more = await screen.findByRole('button', { name: 'Show more' });
    expect(document.querySelector('[data-node-json-preview]')?.textContent).not.toContain('THE_END');
    fireEvent.click(more);
    expect(document.querySelector('[data-node-json-preview]')?.textContent).toContain('THE_END');
  });

  it('shows live JSON without fetching an older persisted result', async () => {
    useExecStreamStore.setState({ wfId: 'wf-preview', status: 'running', perNode: {
      node_3: { status: 'completed', result: '{"answer":42}' },
    } });
    const view = show({ answer: 'outdated' }, 'CodeNode');
    expect(await screen.findByText('42')).toBeInTheDocument();
    expect(view.reads()).toBe(0);
    expect(screen.queryByText('"outdated"')).toBeNull();
    act(() => useExecStreamStore.getState().begin('wf-preview', new AbortController()));
    expect(screen.queryByText('42')).toBeNull();
  });

  it.each(['running', 'error', 'skipped', 'cancelled'])('does not display persisted %s output as a result', async (status) => {
    const view = show({ answer: 'stale' }, 'CodeNode', status);
    await waitFor(() => expect(view.reads()).toBe(1));
    expect(screen.queryByRole('button', { name: 'Output preview' })).toBeNull();
  });

  it('collapses JSON content and reopens without a new request', async () => {
    const view = show({ answer: 42 }, 'CodeNode');
    const button = await screen.findByRole('button', { name: 'Output preview' });
    fireEvent.click(button);
    expect(document.querySelector('[data-node-json-preview]')).toBeNull();
    expect(button.parentElement).toHaveClass('w-56');
    fireEvent.click(button);
    expect(screen.getByText('42')).toBeInTheDocument();
    expect(view.reads()).toBe(1);
  });
});
