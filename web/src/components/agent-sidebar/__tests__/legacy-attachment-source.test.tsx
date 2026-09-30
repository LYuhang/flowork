import '@/lib/i18n';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ContextAttachmentCard } from '../ContextAttachmentCard';
const workspace=vi.hoisted(()=>vi.fn());
vi.mock('@/lib/api/queries/chats',()=>({fetchChatWorkspace:workspace}));

describe('uploaded file source',()=>{
  it('resolves an existing upload only when its details are opened',async()=>{
    workspace.mockResolvedValue({project_id:'project'});
    render(<ContextAttachmentCard originChatId="chat" attachment={{type:'file',name:'report.txt',path:'/chats/chat/attachments/report.txt'}} />);
    expect(workspace).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button',{name:'View context: report.txt'}));
    const link=await screen.findByRole('link',{name:'Open source'});
    expect(workspace).toHaveBeenCalledWith('chat');
    const url=new URL(link.getAttribute('href')!, 'https://example.com');
    expect(url.searchParams.get('projectId')).toBe('project');
    expect(url.searchParams.get('path')).toBe('/chats/chat/attachments/report.txt');
    expect(url.searchParams.get('originChatId')).toBe('chat');
  });
});
