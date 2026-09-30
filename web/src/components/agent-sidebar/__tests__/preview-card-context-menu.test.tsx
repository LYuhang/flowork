import '@/lib/i18n';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { PreviewCardContextMenu } from '../PreviewCardContextMenu';
const { add, workspace, resolve } = vi.hoisted(() => ({ add: vi.fn(), workspace: vi.fn(), resolve: vi.fn() }));
vi.mock('@/lib/api/context-draft', () => ({ addContextToChat: add }));
vi.mock('@/lib/api/queries/chats', () => ({ fetchChatWorkspace: workspace }));
vi.mock('@/lib/api/previews', () => ({ resolvePreview: resolve }));
describe('preview card right-click', () => {
  beforeEach(() => { vi.clearAllMocks(); add.mockResolvedValue({}); });
  it('quotes the displayed workflow version into its source chat', async () => {
    render(<PreviewCardContextMenu chatId="source" workflow={{id:'wf',version:'v2.sv3'}} label="Workflow"><div>Card</div></PreviewCardContextMenu>);
    fireEvent.contextMenu(screen.getByText('Card'));
    fireEvent.click(screen.getByRole('menuitem',{name:'Quote in conversation'}));
    await waitFor(() => expect(add).toHaveBeenCalledOnce());
    expect(add.mock.calls[0][0]).toBe('source');
    expect(add.mock.calls[0][1][0].resource).toEqual({kind:'workflow',workflow_id:'wf',version:'v2.sv3'});
  });
  it('resolves a file in the source workspace and preserves its revision', async () => {
    workspace.mockResolvedValue({project_id:'p'});
    resolve.mockResolvedValue({fileRef:{schemaVersion:1,scope:'project',projectId:'p',path:'/data/a.txt'},revision:'rev',contentType:'text/plain'});
    render(<PreviewCardContextMenu chatId="source" filePath="/data/a.txt" label="File"><div>File card</div></PreviewCardContextMenu>);
    fireEvent.contextMenu(screen.getByText('File card'));
    fireEvent.click(screen.getByRole('menuitem',{name:'Quote in conversation'}));
    await waitFor(() => expect(add).toHaveBeenCalledOnce());
    expect(workspace).toHaveBeenCalledWith('source');
    expect(add.mock.calls[0][1][0].resource.revision).toBe('rev');
  });
});
