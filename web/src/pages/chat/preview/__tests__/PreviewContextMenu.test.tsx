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
  page=3;fireEvent.click(item);
  await waitFor(()=>expect(add).toHaveBeenCalledOnce());
  expect(add.mock.calls[0][0]).toBe('source');
  expect(add.mock.calls[0][1][0].selector.pages).toEqual([2]);
});
