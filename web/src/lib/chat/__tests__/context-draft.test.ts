import { describe, expect, it } from 'vitest';
import { DraftController, mergeDraft, type DraftCache, type DraftValue, type ServerDraft } from '../context-draft';
import type { ChatAttachment } from '@/components/agent-sidebar/chat-attachments';

const quote = (id:string,text=id): ChatAttachment => ({schema_version:1,id,label:'Quote',type:'quote',
  source:{kind:'message',chat_id:'chat',message_id:'m'},snapshot:{text}});
const blank = (): ServerDraft => ({chat_id:'chat',version:0,generation:0,text:'',attachments:[]});
function harness(cache?: DraftCache, discardInitial = false) {
  let remote = blank(), local: DraftValue = cache ? {text:cache.text,attachments:cache.attachments} : {text:'',attachments:[]};
  let saved: DraftCache | undefined, failAfterApply = false, failRead = false;
  const receipts = new Set<string>();
  const controller = new DraftController('chat', {
    get:async () => { if (failRead) { failRead=false; throw new Error('read unavailable'); } return structuredClone(remote); },
    mutate:async op => {
      if (!receipts.has(op.operation_id)) {
        if (op.kind === 'text') {
          if (remote.text !== op.previous_text && remote.text !== op.text) throw new Error('draft_text_changed_in_another_window');
          remote.text = op.text;
        }
        if (op.kind === 'append') for (const item of op.attachments) {
          if (!remote.attachments.some(a => a.id === item.id)) remote.attachments.push(item);
        }
        if (op.kind === 'remove') remote.attachments = remote.attachments.filter(a => !op.attachment_keys.includes(a.id!));
        remote.version++; receipts.add(op.operation_id);
      }
      if (failAfterApply) { failAfterApply = false; throw new Error('network lost'); }
      return structuredClone(remote);
    },
  }, {
    read:() => ({...local,attachments:[...local.attachments]}),write:next => {local=next;},
    save:next => {saved=structuredClone(next);},changed:() => {},
  },cache,discardInitial);
  return {controller,get local(){return local;},set local(value){local=value;},get remote(){return remote;},set remote(value){remote=value;},
    get saved(){return saved!;},loseNextResponse:() => {failAfterApply=true;},loseNextRead:()=>{failRead=true;}};
}

describe('durable context draft synchronization', () => {
  it('discards the initial server draft on reload while keeping new local input', async () => {
    const h = harness(undefined, true);
    h.remote = {...blank(),version:3,text:'unsent before reload',attachments:[quote('old')]};
    h.local = {text:'typed after reload',attachments:[quote('new')]};
    await h.controller.refresh();
    expect(h.local).toEqual({text:'typed after reload',attachments:[quote('new')]});
    expect(h.remote.text).toBe('');expect(h.remote.attachments).toEqual([]);
    await h.controller.flush();
    expect(h.remote.text).toBe('typed after reload');
    expect(h.remote.attachments).toEqual([quote('new')]);
  });
  it('retries an ambiguous discard without resurrecting the old draft', async () => {
    const h = harness(undefined, true);
    h.remote = {...blank(),version:3,text:'old text',attachments:[quote('old')]};
    h.loseNextResponse();
    await expect(h.controller.refresh()).rejects.toThrow('network lost');
    h.remote.attachments.push(quote('late'));
    await h.controller.refresh();
    expect(h.local).toEqual({text:'',attachments:[quote('late')]});
  });

  it('does not append a queued attachment removed while an earlier append is awaiting acknowledgement', async () => {
    let local: DraftValue = { text: '', attachments: [] };
    let remote = blank();
    let release!: () => void;
    let started!: () => void;
    const entered = new Promise<void>(resolve => { started = resolve; });
    const gate = new Promise<void>(resolve => { release = resolve; });
    const appended: string[] = [];
    const controller = new DraftController('chat', {
      get: async () => remote,
      mutate: async operation => {
        if (operation.kind === 'append') {
          appended.push(operation.attachments[0].id!);
          if (appended.length === 1) { started(); await gate; }
          remote = { ...remote, version: remote.version + 1, attachments: [...remote.attachments, ...operation.attachments] };
        }
        return remote;
      },
    }, { read: () => ({ ...local, attachments: [...local.attachments] }), write: value => { local = value; }, save: () => {}, changed: () => {} });
    await controller.refresh();
    local = { text: '', attachments: [quote('first'), quote('removed')] };
    const saving = controller.flush();
    await entered;
    local = { ...local, attachments: [quote('first')] };
    release(); await saving;
    expect(appended).toEqual(['first']);
    expect(local.attachments.map(item => item.id)).toEqual(['first']);
  });

  it('merges another window attachment while preserving local typing', () => {
    const result = mergeDraft({text:'old',attachments:[]},{text:'my edit',attachments:[quote('local')]},
      {text:'old',attachments:[quote('remote')]});
    expect(result.value.text).toBe('my edit');
    expect(result.value.attachments.map(a => a.id)).toEqual(['remote','local']);
    expect(result.conflict).toBe(false);
  });
  it('does not resurrect unchanged stale cached attachments after a send', () => {
    const old={text:'sent',attachments:[quote('sent')]};
    expect(mergeDraft(old,old,blank()).value).toEqual({text:'',attachments:[]});
  });
  it('reports conflicting text rather than overwriting either edit', () => {
    const result=mergeDraft({text:'old',attachments:[]},{text:'local',attachments:[]},{text:'remote',attachments:[]});
    expect(result.conflict).toBe(true); expect(result.value.text).toBe('local');
  });
  it('syncs file/quote additions and text, then consumes only sent attachments', async () => {
    const h=harness(); await h.controller.refresh();
    h.local={text:'question',attachments:[quote('a')]}; await h.controller.flush();
    expect(h.remote.text).toBe('question'); expect(h.local.attachments.map(a=>a.id)).toEqual(['a']);
    h.controller.beginSend(); h.local={text:'next question',attachments:[quote('next')]};
    h.remote={...blank(),version:4,generation:1,attachments:[quote('from-preview')]};
    await h.controller.finishSend(true);
    expect(h.local.text).toBe('next question');
    expect(h.local.attachments.map(a=>a.id)).toEqual(['from-preview','next']);
    expect(h.controller.conflict).toBe(false);
  });
  it('does not restore a submitted draft when its acceptance response was lost', async () => {
    const h=harness(); await h.controller.refresh();
    h.local={text:'already sent',attachments:[quote('sent')]}; await h.controller.flush();
    h.controller.beginSend(); h.local={text:'next draft',attachments:[quote('next')]};
    h.remote={...blank(),version:4,generation:1};
    await h.controller.finishSend(false);
    expect(h.local).toEqual({text:'next draft',attachments:[quote('next')]});
    await h.controller.flush();
    expect(h.remote.text).toBe('next draft');
    expect(h.remote.attachments.map(a=>a.id)).toEqual(['next']);
  });
  it('retains its submission receipt until acceptance can be checked', async () => {
    const h=harness(); await h.controller.refresh();
    h.local={text:'sent',attachments:[quote('sent')]}; await h.controller.flush();
    h.controller.beginSend(); h.local={text:'',attachments:[]};
    h.remote={...blank(),version:4,generation:1}; h.loseNextRead();
    await expect(h.controller.finishSend(false)).rejects.toThrow('read unavailable');
    expect(h.saved.submission?.text).toBe('sent');
    await h.controller.flush();
    expect(h.local).toEqual({text:'',attachments:[]});
    expect(h.saved.submission).toBeUndefined();
  });
  it('restores rejected submission while retaining newly typed text', async () => {
    const h=harness(); await h.controller.refresh();
    h.local={text:'unsent',attachments:[quote('sent')]}; await h.controller.flush();
    h.controller.beginSend(); h.local={text:'next draft',attachments:[quote('next')]};
    await h.controller.finishSend(false);
    expect(h.local.text).toBe('unsent\n\nnext draft');
    expect(h.local.attachments.map(a=>a.id)).toEqual(['sent','next']);
  });
  it('retries a lost response with its original operation receipt after consumption', async () => {
    const h=harness(); await h.controller.refresh();
    h.local={text:'',attachments:[quote('a')]};h.loseNextResponse();
    await expect(h.controller.flush()).rejects.toThrow('network lost');
    const operation=h.saved.pending?.operation_id;
    expect(operation).toBeTruthy();
    h.remote={...blank(),generation:1,version:2};
    await h.controller.refresh();
    expect(h.local.attachments).toEqual([]);
    expect(h.remote.attachments).toEqual([]);
  });
  it('preserves typing and allows explicit conflict resolution after a CAS failure', async () => {
    const h=harness();await h.controller.refresh();h.local={text:'my text',attachments:[]};
    h.remote={...blank(),text:'other window',version:1};
    await expect(h.controller.flush()).rejects.toThrow('draft_text_changed');
    expect(h.local.text).toBe('my text');expect(h.controller.conflict).toBe(true);
    await h.controller.chooseText(true);expect(h.remote.text).toBe('my text');
  });
  it('restores a pending send on refresh if the server never accepted it', async () => {
    const old={...blank(),text:'unsent',attachments:[quote('a')],version:2};
    const h=harness({text:'',attachments:[],remote:old,submission:{text:'unsent',attachments:[quote('a')],generation:0}});
    h.remote=old;await h.controller.refresh();
    expect(h.local).toEqual({text:'unsent',attachments:[quote('a')]});
  });
  it('keeps the next draft when a refreshed pending send was already accepted', async () => {
    const old={...blank(),text:'sent',attachments:[quote('a')],version:2};
    const h=harness({text:'next',attachments:[quote('b')],remote:old,submission:{text:'sent',attachments:[quote('a')],generation:0}});
    h.remote={...blank(),version:3,generation:1};await h.controller.refresh();
    expect(h.local).toEqual({text:'next',attachments:[quote('b')]});
  });
  it('keeps a conflict sticky across unrelated attachment updates', async () => {
    const h=harness();await h.controller.refresh();h.local={text:'mine',attachments:[]};
    h.remote={...blank(),version:1,text:'theirs'};
    await h.controller.refresh();expect(h.controller.conflict).toBe(true);
    h.remote={...h.remote,version:2,attachments:[quote('external')]};
    await h.controller.refresh();expect(h.controller.conflict).toBe(true);
    expect(h.local.text).toBe('mine');
  });

  it('does not resurrect private cached data after an account reset', async () => {
    const h=harness();await h.controller.refresh();h.controller.dispose();
    h.local={text:'new account',attachments:[]};
    await expect(h.controller.flush()).rejects.toThrow('session_changed');
    expect(h.remote.text).toBe('');
  });

});
