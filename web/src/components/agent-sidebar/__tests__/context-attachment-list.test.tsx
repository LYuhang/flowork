import '@/lib/i18n';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ContextAttachmentList } from '../ContextAttachmentList';
import type { ChatAttachment } from '../chat-attachments';

describe('sent context attachment list', () => {
  it('bounds mounted cards and mounts full quote contents only when opened', () => {
    const attachments: ChatAttachment[] = Array.from({length:32}, (_, i) => ({
      schema_version:1, id:`quote-${i}`, type:'quote', label:`Excerpt ${i}`,
      source:{kind:'message',chat_id:'chat',message_id:`message-${i}`},
      snapshot:{text:'A long quotation '.repeat(1000)},
    }));
    const { container } = render(<ContextAttachmentList attachments={attachments} />);
    const count = () => container.querySelectorAll('[data-role="context-attachment-card"]').length;
    expect(count()).toBe(4);
    expect(container.textContent!.length).toBeLessThan(2000);
    fireEvent.click(screen.getByRole('button',{name:'Show 28 more attachments'}));
    expect(count()).toBe(32);
    expect(screen.getByRole('button',{name:'Collapse attachments'})).toHaveAttribute('aria-expanded','true');
    fireEvent.click(screen.getByRole('button',{name:'Collapse attachments'}));
    expect(count()).toBe(4);
    fireEvent.click(screen.getByRole('button',{name:'View context: Excerpt 0'}));
    expect(screen.getByRole('dialog').textContent).toContain('A long quotation '.repeat(1000));
  });
});
