import '@/lib/i18n';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { expect, it, vi } from 'vitest';
import { PreviewOriginProvider } from '@/lib/preview/PreviewOriginProvider';
import { PreviewContextMenu } from '../PreviewContextMenu';
const {add,get}=vi.hoisted(()=>({add:vi.fn(),get:vi.fn()}));
vi.mock('@/lib/api/context-draft',()=>({addContextToChat:add,fetchContextDraft:get}));
it('freezes a content reference at right click and shows no permanent Quote button',async()=>{
  get.mockResolvedValue({});add.mockResolvedValue({});let page=2;
  render(<QueryClientProvider client={new QueryClient()}><PreviewOriginProvider origin={{chatId:'source'}}>
    <PreviewContextMenu label="Quote current page" build={()=>({schema_version:1,id:'page',type:'resource',label:'Document',resource:{kind:'file',file_ref:{schemaVersion:1,scope:'project',projectId:'p',path:'/data/a.pdf'}},selector:{kind:'pages',pages:[page]}})}><span>Actual page</span></PreviewContextMenu>
  </PreviewOriginProvider></QueryClientProvider>);
  expect(screen.queryByText('Quote current page')).toBeNull();
  fireEvent.contextMenu(screen.getByText('Actual page'));
  const item=await screen.findByRole('menuitem',{name:'Quote current page'});
  await waitFor(()=>expect(item).not.toHaveAttribute('aria-disabled','true'));
  fireEvent.pointerUp(item, { button: 2 });
  expect(add).not.toHaveBeenCalled();
  page=3;fireEvent.click(item);
  await waitFor(()=>expect(add).toHaveBeenCalledOnce());
  expect(add.mock.calls[0][0]).toBe('source');
  expect(add.mock.calls[0][1][0].selector.pages).toEqual([2]);
});

it('prepares a reference when touch long-press opens without contextmenu', async () => {
  get.mockResolvedValue({}); add.mockClear(); add.mockResolvedValue({});
  let selected = 'before selection';
  const outerBuild = vi.fn();
  const build = vi.fn(() => ({
    schema_version: 1 as const, id: 'touch', type: 'resource' as const, label: selected,
    resource: { kind: 'file' as const, file_ref: { schemaVersion: 1 as const, scope: 'project' as const, projectId: 'p', path: '/data/a.pdf' } },
    selector: { kind: 'pages' as const, pages: [2] },
  }));
  render(<QueryClientProvider client={new QueryClient()}><PreviewOriginProvider origin={{chatId:'source'}}>
    <PreviewContextMenu label="Outer quote" build={outerBuild}>
      <PreviewContextMenu label="Touch quote" build={build}><span>Touch page</span></PreviewContextMenu>
    </PreviewContextMenu>
  </PreviewOriginProvider></QueryClientProvider>);
  fireEvent.pointerDown(screen.getByText('Touch page'), { pointerType: 'touch', clientX: 12, clientY: 24 });
  expect(build).not.toHaveBeenCalled();
  selected = 'selection after pointer down';
  const item = await screen.findByRole('menuitem', { name: 'Touch quote' }, { timeout: 2000 });
  await waitFor(() => expect(item).not.toHaveAttribute('aria-disabled', 'true'));
  expect(build).toHaveBeenCalledOnce();
  expect(outerBuild).not.toHaveBeenCalled();
  expect(screen.getAllByRole('menuitem')).toHaveLength(1);
  fireEvent.click(item);
  await waitFor(() => expect(add).toHaveBeenCalledOnce());
  expect(add.mock.calls[0][1][0].label).toBe('selection after pointer down');
});
