import { useEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Copy, Quote } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import type { components } from '@/lib/api/schema';
import { addContextToChat } from '@/lib/api/context-draft';
import { copyText } from '@/lib/api/chat-engagement';

type QuoteAttachment = components['schemas']['QuoteContextAttachment'];

export function buildMessageQuote(chatId: string, messageId: string, text: string, label: string): QuoteAttachment {
  if (!text.trim()) throw new Error('empty_quote');
  if (text.length > 32768) throw new Error('quote_too_large');
  return {schema_version:1,id:crypto.randomUUID(),type:'quote',label,
    source:{kind:'message',chat_id:chatId,message_id:messageId},snapshot:{text}};
}
function focusComposer(chatId: string) {
  Array.from(document.querySelectorAll<HTMLTextAreaElement>('textarea[data-context-chat]'))
    .find(element => element.dataset.contextChat === chatId)?.focus();
}

let keyboardSelectionUsers = 0;
function openKeyboardSelectionMenu(event: KeyboardEvent) {
  if (event.key !== 'ContextMenu' && !(event.key === 'F10' && event.shiftKey)) return;
  const selected = window.getSelection();
  if (!selected || selected.isCollapsed || !selected.rangeCount) return;
  const range = selected.getRangeAt(0);
  const start = range.startContainer.nodeType === Node.ELEMENT_NODE
    ? range.startContainer as Element : range.startContainer.parentElement;
  if (!start?.closest('[data-quotable-message]')) return;
  const rect = range.getClientRects?.()[0] ?? range.getBoundingClientRect();
  const menuEvent = new MouseEvent('contextmenu', { bubbles: true, cancelable: true,
    clientX: rect.left, clientY: rect.bottom });
  start.dispatchEvent(menuEvent);
  if (menuEvent.defaultPrevented) {
    event.preventDefault();
    requestAnimationFrame(() => document.querySelector<HTMLButtonElement>('[data-role="message-quote-menu"] button')?.focus());
  }
}

/** Selection events are scoped to one rendered message. No history-wide scan
 * or per-token selectionchange listener. The captured text survives focus. */
export function MessageQuoteSelection({chatId,messageId,label,enabled,children}: {
  chatId: string | null; messageId?: string; label: string; enabled: boolean; children: ReactNode;
}) {
  const {t} = useTranslation();
  const root = useRef<HTMLDivElement>(null);
  const [selection,setSelection] = useState<{attachment:QuoteAttachment;left:number;top:number} | null>(null);
  const [busy,setBusy] = useState(false);
  useEffect(() => {
    // One keyboard listener for the whole transcript, regardless of its size.
    if (keyboardSelectionUsers++ === 0) document.addEventListener('keydown', openKeyboardSelectionMenu);
    return () => { if (--keyboardSelectionUsers === 0) document.removeEventListener('keydown', openKeyboardSelectionMenu); };
  }, []);
  const capture = (position?: {left: number; top: number}) => {
    if (!enabled || !chatId || !messageId || !root.current) return;
    const selected = window.getSelection();
    if (!selected || selected.isCollapsed || !selected.rangeCount) {setSelection(null);return;}
    const range = selected.getRangeAt(0);
    if (!root.current.contains(range.startContainer) || !root.current.contains(range.endContainer)) {
      setSelection(null);
      toast.info(t('composer.context.singleMessage','Select text within one message at a time.'));
      return;
    }
    const parent = range.startContainer.nodeType === Node.ELEMENT_NODE ? range.startContainer as Element : range.startContainer.parentElement;
    if (parent?.closest('button,input,textarea,[contenteditable="true"]')) return;
    try {
      const attachment = buildMessageQuote(chatId,messageId,selected.toString(),label);
      const rect = range.getBoundingClientRect();
      setSelection({attachment,left:Math.min(Math.max(8,position?.left ?? rect.left),Math.max(8,window.innerWidth-250)),
        top:Math.max(8,Math.min(window.innerHeight-50,position?.top ?? (rect.top >= 48 ? rect.top-44 : rect.bottom+8)))});
      return true;
    } catch {
      setSelection(null);
      toast.error(t('composer.context.tooLarge','Select a smaller excerpt (up to 32,768 characters).'));
    }
  };
  useEffect(() => {
    if (!selection) return;
    const close = () => setSelection(null);
    const key = (event: KeyboardEvent) => {if(event.key === 'Escape') close();};
    const outside = (event:PointerEvent) => {
      if (!(event.target as Element)?.closest?.('[data-role="message-quote-menu"]')) close();
    };
    window.addEventListener('scroll',close,true);window.addEventListener('resize',close);
    document.addEventListener('keydown',key);document.addEventListener('pointerdown',outside);
    return () => {window.removeEventListener('scroll',close,true);window.removeEventListener('resize',close);
      document.removeEventListener('keydown',key);document.removeEventListener('pointerdown',outside);};
  },[selection]);
  const add = async () => {
    if (!selection || busy || !chatId) return;
    setBusy(true);
    try {
      await addContextToChat(chatId,[selection.attachment],selection.attachment.id);
      setSelection(null);window.getSelection()?.removeAllRanges();focusComposer(chatId);
      toast.success(t('composer.context.added','Added to the conversation draft'));
    } catch {toast.error(t('composer.context.addFailed','Could not add this reference. Its source may be unavailable; retry without losing your selection.'));}
    finally {setBusy(false);}
  };
  return <>
    <div ref={root} data-quotable-message={messageId} onPointerUp={event => {if (event.pointerType === 'touch' && window.getSelection()?.type === 'Range') capture();}}
      onContextMenu={event => {
        if (capture({left:event.clientX,top:event.clientY})) event.preventDefault();
      }} onKeyUp={event => {if (event.key.startsWith('Arrow') && event.shiftKey) capture();}}>{children}</div>
    {selection && createPortal(<div role="toolbar" aria-label={t('composer.context.selectionActions','Selected text actions')}
      data-role="message-quote-menu" className="fixed z-[100] flex items-center gap-1 rounded-xl border bg-popover p-1 text-popover-foreground shadow-lg"
      style={{left:selection.left,top:selection.top}} onMouseDown={event => event.preventDefault()}>
      <button type="button" disabled={busy} onClick={() => void add()} className="inline-flex h-8 items-center gap-1.5 rounded-lg px-2 text-xs hover:bg-accent disabled:opacity-50">
        <Quote className="h-3.5 w-3.5" />{t('composer.context.quote','Quote in conversation')}
      </button>
      <button type="button" onClick={() => void copyText(selection.attachment.snapshot.text).then(() => setSelection(null)).catch(() => toast.error(t('chat.actions.copyFailed','Could not copy. Please try again.')))}
        className="inline-flex h-8 items-center gap-1.5 rounded-lg px-2 text-xs hover:bg-accent"><Copy className="h-3.5 w-3.5" />{t('copy','Copy')}</button>
    </div>,document.body)}
  </>;
}
