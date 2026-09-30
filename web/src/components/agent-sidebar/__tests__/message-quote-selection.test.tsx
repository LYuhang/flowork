import '@/lib/i18n';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MessageQuoteSelection } from '../MessageQuoteSelection';

const {addContext,copy} = vi.hoisted(() => ({addContext:vi.fn(),copy:vi.fn()}));
vi.mock('@/lib/api/chat-engagement', () => ({copyText:copy}));
vi.mock('@/lib/api/context-draft', () => ({addContextToChat:addContext}));
function select(node: Node, start=0, end=node.textContent!.length) {
  const range=document.createRange();range.setStart(node,start);range.setEnd(node,end);
  range.getBoundingClientRect=() => ({left:100,top:100,bottom:120,right:180,width:80,height:20,x:100,y:100,toJSON:()=>({})});
  window.getSelection()!.removeAllRanges();window.getSelection()!.addRange(range);
}
function message() {
  return render(<MessageQuoteSelection chatId="source-chat" messageId="message-1" label="Excerpt" enabled>
    <pre data-testid="source">{'line one\n    code line two'}</pre>
  </MessageQuoteSelection>);
}

describe('message quote actions', () => {
  beforeEach(() => {copy.mockReset();copy.mockResolvedValue(undefined);addContext.mockReset();addContext.mockResolvedValue({});});
  afterEach(() => window.getSelection()?.removeAllRanges());
  it('freezes selected text and sends a draft operation without sending an Agent turn', async () => {
    message();const target=screen.getByTestId('source');select(target.firstChild!,9);
    fireEvent.contextMenu(target);
    const quote=screen.getByRole('button',{name:'Quote in conversation'});
    // Browser focus may clear the live selection: the menu uses its snapshot.
    window.getSelection()?.removeAllRanges();fireEvent.click(quote);
    await waitFor(() => expect(addContext).toHaveBeenCalledOnce());
    const [chat,attachments,operation]=addContext.mock.calls[0];
    expect(chat).toBe('source-chat');expect(attachments[0].source.message_id).toBe('message-1');
    expect(attachments[0].snapshot.text).toBe('    code line two');
    expect(operation).toBe(attachments[0].id);
  });
  it('does not attribute a cross-message selection to one message', () => {
    const view=message();const outside=document.createElement('span');outside.textContent='other message';document.body.append(outside);
    const range=document.createRange();range.setStart(screen.getByTestId('source').firstChild!,0);range.setEnd(outside.firstChild!,5);
    window.getSelection()!.addRange(range);fireEvent.contextMenu(screen.getByTestId('source'));
    expect(screen.queryByRole('toolbar')).toBeNull();expect(addContext).not.toHaveBeenCalled();
    outside.remove();view.unmount();
  });
  it('opens the selected excerpt menu from the document keyboard shortcut', async () => {
    message(); const target=screen.getByTestId('source'); select(target.firstChild!,0,8);
    fireEvent.keyDown(document,{key:'F10',shiftKey:true});
    const quote=await screen.findByRole('button',{name:'Quote in conversation'});
    fireEvent.click(quote);
    await waitFor(()=>expect(addContext).toHaveBeenCalledOnce());
    expect(addContext.mock.calls[0][1][0].snapshot.text).toBe('line one');
  });
  it('dismisses selection actions with Escape', () => {
    message();const target=screen.getByTestId('source');select(target.firstChild!);fireEvent.contextMenu(target);
    expect(screen.getByRole('toolbar')).toBeInTheDocument();fireEvent.keyDown(document,{key:'Escape'});
    expect(screen.queryByRole('toolbar')).toBeNull();
  });
  it('copies only the selected excerpt', async () => {
    message();const target=screen.getByTestId('source');select(target.firstChild!,0,4);
    fireEvent.contextMenu(target);fireEvent.click(screen.getByRole('button',{name:'Copy'}));
    await waitFor(() => expect(copy).toHaveBeenCalledWith('line'));
    expect(addContext).not.toHaveBeenCalled();
  });
  it('does not offer a quote without a selection', () => {
    message();fireEvent.contextMenu(screen.getByTestId('source'));
    expect(screen.queryByRole('toolbar')).toBeNull();
    expect(addContext).not.toHaveBeenCalled();
  });
  it('retries the same excerpt operation after a failure', async () => {
    addContext.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({});
    message();const target=screen.getByTestId('source');select(target.firstChild!,0,4);
    fireEvent.contextMenu(target);
    const button=screen.getByRole('button',{name:'Quote in conversation'});
    fireEvent.click(button);await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);await waitFor(() => expect(addContext).toHaveBeenCalledTimes(2));
    expect(addContext.mock.calls[0][2]).toBe(addContext.mock.calls[1][2]);
    expect(addContext.mock.calls[1][1][0].snapshot.text).toBe('line');
  });
});
