import { toast } from 'sonner';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { ChatComposer } from '@/components/agent-sidebar/ChatComposer';
import { MessageItem } from '@/components/agent-sidebar/MessageItem';
import {
  emphasizeUserText,
  findAttachmentMention,
  insertAttachmentMention,
  isFileAttachment,
} from '@/components/agent-sidebar/chat-attachments';
import { useChatStreamStore } from '@/stores/chat-stream';
import { server } from '@/__tests__/msw-handlers';
import { addContextToChat, fetchContextDraft } from '@/lib/api/context-draft';

const SCOPE = '__chat_test';
const CHAT = 'chat_attachment_test';

const { uploadChatAttachmentMock } = vi.hoisted(() => ({
  uploadChatAttachmentMock: vi.fn(),
}));

vi.mock('@/lib/api/queries/chats', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api/queries/chats')>();
  return { ...actual, uploadChatAttachment: uploadChatAttachmentMock };
});

function renderComposer(persisted = false, options: { path?: string; historyReady?: boolean } = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[options.path ?? "/chat"]}>
        <ChatComposer wfId={SCOPE} chatId={CHAT} chatPersisted={persisted} historyReady={options.historyReady} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('chat attachments', () => {
  beforeEach(() => {
    localStorage.clear();
    useChatStreamStore.getState().reset();
    uploadChatAttachmentMock.mockReset();
    uploadChatAttachmentMock.mockImplementation(async ({ file, type }) => ({
      type,
      name: file.name,
      path: `/data/attachments/${crypto.randomUUID()}_${file.name}`,
      content_type: file.type,
      size_bytes: file.size,
    }));
    server.use(
      http.get('*/api/v1/chats/bootstrap', () => HttpResponse.json({
        carrier_scope_id: SCOPE,
        surface: 'chat',
        available_commands: [],
        debug_view_enabled: false,
      })),
      http.get('*/api/v1/chat-scopes/:scopeId/chats/:chatId/state', () => HttpResponse.json({
        todo_items: [],
        background_jobs: [],
        active_modes: [],
        mcp_server_ids: [],
        mcp_config_revision: 0,
      })),
    );
  });

  it.each([
    {path:'/workflow/example/version/v1.sv1', historyReady:true},
    {path:'/chat', historyReady:false},
  ])('explains rejected file drops when the composer is unavailable: %o', async options => {
    const error = vi.spyOn(toast, 'error');
    const {container} = renderComposer(false, options);
    fireEvent.drop(container.querySelector('[data-role="agent-composer-dropzone"]')!, {
      dataTransfer:{files:[new File(['text'],'unavailable.txt',{type:'text/plain'})],types:['Files']},
    });
    expect(uploadChatAttachmentMock).not.toHaveBeenCalled();
    expect(error).toHaveBeenCalledWith(expect.stringContaining('not ready for attachments'));
    error.mockRestore();
  });

  it('keeps a file dropped during streaming in the next draft', async () => {
    useChatStreamStore.getState().setState('streaming', CHAT);
    const {container} = renderComposer();
    fireEvent.drop(container.querySelector('[data-role="agent-composer-dropzone"]')!, {
      dataTransfer:{files:[new File(['next turn'],'next.txt',{type:'text/plain'})],types:['Files']},
    });
    await waitFor(() => expect(container.querySelector('[data-role="agent-composer-attachment-chip"]')).toHaveTextContent('next.txt'));
    expect(uploadChatAttachmentMock).toHaveBeenCalledOnce();
    expect(useChatStreamStore.getState().state).toBe('streaming');
    expect(useChatStreamStore.getState().lastInput).toBeFalsy();
  });

  it('uses one upload pipeline for picker, paste, and drag/drop, then completes @ mentions', async () => {
    const { container } = renderComposer();
    const input = screen.getByRole('textbox');
    const fileInput = container.querySelector('[data-role="agent-composer-file-input"]') as HTMLInputElement;
    const pickerFile = new File(['doc'], 'brief.pdf', { type: 'application/pdf' });
    fireEvent.change(fileInput, { target: { files: [pickerFile] } });

    const pastedImage = new File(['img'], 'photo.png', { type: 'image/png' });
    fireEvent.paste(input, { clipboardData: { files: [pastedImage] } });

    const droppedVideo = new File(['vid'], 'clip.mp4', { type: 'video/mp4' });
    const dropzone = container.querySelector('[data-role="agent-composer-dropzone"]') as HTMLElement;
    fireEvent.drop(dropzone, {
      dataTransfer: { files: [droppedVideo], types: ['Files'] },
    });

    await waitFor(() => {
      expect(container.querySelectorAll('[data-role="agent-composer-attachment-chip"]')).toHaveLength(3);
    });
    const uploaded = Object.values(useChatStreamStore.getState().pendingAttachments).flat();
    expect(uploadChatAttachmentMock).toHaveBeenCalledTimes(3);
    expect(uploaded).toEqual(expect.arrayContaining([
      expect.objectContaining({ name: 'brief.pdf', type: 'file' }),
      expect.objectContaining({ name: 'photo.png', type: 'image' }),
      expect.objectContaining({ name: 'clip.mp4', type: 'video' }),
    ]));

    await userEvent.type(input, 'Review@pho');
    const mention = await screen.findByRole('option', { name: /photo\.png/i });
    await userEvent.click(mention);
    expect(input).toHaveValue('Review@photo.png ');
  });

  it('serializes a multi-file picker selection and preserves its attachment order', async () => {
    const { container } = renderComposer();
    const fileInput = container.querySelector('[data-role="agent-composer-file-input"]') as HTMLInputElement;
    const firstFile = new File(['first'], 'first.csv', { type: 'text/csv' });
    const secondFile = new File(['second'], 'second.md', { type: 'text/markdown' });
    let releaseFirst!: (attachment: {
      type: 'file';
      name: string;
      path: string;
      content_type: string;
      size_bytes: number;
    }) => void;

    uploadChatAttachmentMock
      .mockImplementationOnce(() => new Promise((resolve) => {
        releaseFirst = resolve;
      }))
      .mockImplementationOnce(async () => ({
        type: 'file',
        name: secondFile.name,
        path: `/data/attachments/${secondFile.name}`,
        content_type: secondFile.type,
        size_bytes: secondFile.size,
      }));

    fireEvent.change(fileInput, { target: { files: [firstFile, secondFile] } });
    await waitFor(() => expect(uploadChatAttachmentMock).toHaveBeenCalledTimes(1));

    releaseFirst({
      type: 'file',
      name: firstFile.name,
      path: `/data/attachments/${firstFile.name}`,
      content_type: firstFile.type,
      size_bytes: firstFile.size,
    });

    await waitFor(() => {
      expect(uploadChatAttachmentMock).toHaveBeenCalledTimes(2);
      expect(container.querySelectorAll('[data-role="agent-composer-attachment-chip"]')).toHaveLength(2);
    });
    expect(Object.values(useChatStreamStore.getState().pendingAttachments)
      .flat()
      .filter(isFileAttachment).map((item) => item.name))
      .toEqual(['first.csv', 'second.md']);
  });

  it('retains a failed file for retry and holds later files in selection order', async () => {
    uploadChatAttachmentMock.mockRejectedValueOnce(new Error('temporary network failure'));
    const { container } = renderComposer();
    const picker = container.querySelector('[data-role="agent-composer-file-input"]')!;
    fireEvent.change(picker, { target: { files: [new File(['a'], 'a.txt'), new File(['b'], 'b.txt')] } });
    const retry = await screen.findByRole('button', { name: 'Retry upload' });
    expect(uploadChatAttachmentMock).toHaveBeenCalledTimes(1);
    expect(container.querySelectorAll('[data-role="agent-composer-attachment-uploading"]')).toHaveLength(2);
    fireEvent.click(retry);
    await waitFor(() => expect(container.querySelectorAll('[data-role="agent-composer-attachment-chip"]')).toHaveLength(2));
    expect(Object.values(useChatStreamStore.getState().pendingAttachments).flat().filter(isFileAttachment).map(item => item.name)).toEqual(['a.txt', 'b.txt']);
  });

  it('aborts an in-flight upload without attaching its late result and continues the queue', async () => {
    let release!: (value: unknown) => void;
    uploadChatAttachmentMock.mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
    const { container } = renderComposer();
    const picker = container.querySelector('[data-role="agent-composer-file-input"]')!;
    fireEvent.change(picker, { target: { files: [new File(['a'], 'cancel.txt')] } });
    fireEvent.change(picker, { target: { files: [new File(['b'], 'keep.txt')] } });
    await waitFor(() => expect(uploadChatAttachmentMock).toHaveBeenCalledTimes(1));
    const signal = uploadChatAttachmentMock.mock.calls[0][0].signal as AbortSignal;
    fireEvent.click(screen.getAllByRole('button', { name: 'Cancel upload' })[0]);
    expect(signal.aborted).toBe(true);
    release({ type: 'file', name: 'cancel.txt', path: '/data/cancel.txt', content_type: 'text/plain' });
    await waitFor(() => expect(container.querySelectorAll('[data-role="agent-composer-attachment-chip"]')).toHaveLength(1));
    expect(Object.values(useChatStreamStore.getState().pendingAttachments).flat().filter(isFileAttachment).map(item => item.name)).toEqual(['keep.txt']);
  });

  it('renders commands as ordinary text and emphasizes only durable attachments', () => {
    render(
      <MessageItem
        message={{
          role: 'user',
          content: '/workflow compare @photo.png with @not-attached',
          tool_calls: [],
          attachments: [{
            type: 'image',
            name: 'photo.png',
            path: '/data/attachments/photo.png',
            content_type: 'image/png',
            size_bytes: 3,
          }],
        }}
      />,
    );
    expect(screen.getByText(/\/workflow compare/)).not.toHaveAttribute('data-token-kind');
    expect(screen.getByText('@photo.png')).toHaveAttribute('data-token-kind', 'attachment');
    expect(screen.getByText(/@not-attached/).tagName).toBe('SPAN');
  });

  it('keeps mention parsing and replacement independent from rendering', () => {
    const query = findAttachmentMention('Look at @rep', 12);
    expect(query).toEqual({ start: 8, end: 12, query: 'rep' });
    expect(insertAttachmentMention('Look at @rep now', query!, 'report 1.csv')).toEqual({
      value: 'Look at @report 1.csv  now',
      caret: 22,
    });
    expect(emphasizeUserText('@random', [])).toEqual([
      { text: '@random', emphasized: false },
    ]);
    const direct = '请查看@pho';
    expect(findAttachmentMention(direct, direct.length)).toEqual({
      start: 3,
      end: direct.length,
      query: 'pho',
    });
  });
  it('preserves native text drop instead of treating it as an upload', () => {
    const { container } = renderComposer();
    const target = container.querySelector('[data-role="agent-composer-dropzone"]')!;
    const event = new Event('drop', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'dataTransfer', { value: {types:['text/plain'],files:[]} });
    target.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    expect(uploadChatAttachmentMock).not.toHaveBeenCalled();
  });

  it('does not silently upload a directory as an empty file', () => {
    const { container } = renderComposer();
    const target = container.querySelector('[data-role="agent-composer-dropzone"]')!;
    fireEvent.drop(target, {dataTransfer:{types:['Files'],files:[new File([], 'folder')],
      items:[{webkitGetAsEntry:() => ({isDirectory:true})}]}});
    expect(uploadChatAttachmentMock).not.toHaveBeenCalled();
  });

  it('opens a saved quote on demand and preserves its line breaks', async () => {
    const text = 'first line\n    code indentation\n' + 'long excerpt '.repeat(60);
    render(<MessageItem message={{role:'user',content:'Explain this',tool_calls:[],attachments:[{
      schema_version:1,type:'quote',id:'quote-one',label:'Selected message',
      source:{kind:'message',chat_id:CHAT,message_id:'m1'},snapshot:{text},
    }]}} />);
    expect(screen.queryByRole('dialog')).toBeNull();
    await userEvent.click(screen.getByRole('button', {name:'View context: Selected message'}));
    const dialog = await screen.findByRole('dialog');
    expect(dialog.querySelector('pre')?.textContent).toBe(text);
  });

  it('merges an external Preview reference into a persisted draft without sending or replacing its text', async () => {
    const {container}=renderComposer(true);
    const input=screen.getByRole('textbox');
    fireEvent.change(input,{target:{value:'Keep this question'}});
    await waitFor(async () => expect((await fetchContextDraft(CHAT)).text).toBe('Keep this question'),{timeout:2500});
    await addContextToChat(CHAT,[{schema_version:1,type:'quote',id:'external',label:'Preview excerpt',
      source:{kind:'message',chat_id:CHAT,message_id:'source'},snapshot:{text:'specific context'}}]);
    await screen.findByRole('button',{name:'View context: Preview excerpt'});
    expect(input).toHaveValue('Keep this question');
    expect(useChatStreamStore.getState().runtimes[CHAT]?.state).not.toBe('streaming');
    fireEvent.click(container.querySelector('[data-action="agent-composer-attachment-remove"]')!);
    await waitFor(async () => expect((await fetchContextDraft(CHAT)).attachments).toHaveLength(0));
    expect(input).toHaveValue('Keep this question');
  });

  it.each(['/', '/s', '/skill', '/skill-use'])('selects a Skill from %s without retaining the trigger and permits cancellation', async (trigger) => {
    server.use(
      http.get('*/api/v1/chats/bootstrap', () => HttpResponse.json({carrier_scope_id:SCOPE,surface:'chat',available_commands:['skill']})),
      http.get('*/api/v1/skills', () => HttpResponse.json({items:[{id:'22222222-2222-4222-8222-222222222222',name:'research',source:'custom',access:{capabilities:['use']}}]})),
    );
    renderComposer();
    const input=screen.getByRole('textbox');
    fireEvent.change(input,{target:{value:trigger}});
    await userEvent.click(await screen.findByRole('option',{name:/\/skill-use/}));
    const frames: FrameRequestCallback[] = [];
    const animation = vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => {
      frames.push(callback);
      return frames.length;
    });
    await userEvent.click(await screen.findByRole('button',{name:/research/}));
    expect(input).toHaveValue('/skill-use:[research] ');
    expect(useChatStreamStore.getState().runtimes[CHAT]?.state).not.toBe('streaming');
    fireEvent.change(input,{target:{value:'/skill'}});
    await userEvent.click(await screen.findByRole('option',{name:/\/skill-use/}));
    // Reopen before the previous selection's delayed focus callback runs.
    animation.mockRestore();
    act(() => { for (const callback of frames) callback(0); });
    expect(screen.getByRole('textbox', {name:'Search Skills'})).toHaveFocus();
    await userEvent.keyboard('{Escape}');
    // Choosing the first-level command inserts its canonical spelling.
    // Cancelling the picker preserves that command for further editing.
    expect(input).toHaveValue('/skill-use ');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('uses a second-level goal menu and writes the bare goal command for a new objective', async () => {
    server.use(http.get('*/api/v1/chats/bootstrap', () => HttpResponse.json({carrier_scope_id:SCOPE,surface:'chat',available_commands:['goal']})));
    renderComposer();
    const input=screen.getByRole('textbox');
    fireEvent.change(input,{target:{value:'/'}});
    await userEvent.click(await screen.findByRole('option',{name:/\/goal/}));
    await userEvent.click(await screen.findByRole('button',{name:/None/}));
    expect(input).toHaveValue('/goal ');
    expect(useChatStreamStore.getState().runtimes[CHAT]?.state).not.toBe('streaming');
    fireEvent.change(input,{target:{value:'/goal'}});
    await userEvent.click(await screen.findByRole('option',{name:/\/goal/}));
    await userEvent.click(await screen.findByRole('button',{name:'Resume goal'}));
    expect(input).toHaveValue('/goal:resume ');
  });

});
