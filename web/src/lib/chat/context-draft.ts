import type { components } from '@/lib/api/schema';
import { contextAttachmentKey, type ChatAttachment } from '@/components/agent-sidebar/chat-attachments';

export const CONTEXT_DRAFT_RESET_EVENT = 'flowork:reset-context-drafts';

export interface DraftValue { text: string; attachments: ChatAttachment[] }
export interface ServerDraft extends DraftValue { chat_id: string; version: number; generation: number }
export type DraftOperation = components['schemas']['DraftAppend'] | components['schemas']['DraftRemove'] | components['schemas']['DraftText'];
export interface DraftCache extends DraftValue { remote?: ServerDraft; pending?: DraftOperation; submission?: DraftValue & {generation:number} }
export interface DraftTransport {
  get(): Promise<ServerDraft>;
  mutate(operation: DraftOperation): Promise<ServerDraft>;
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value)
    .filter(([key,value]) => value != null && !(key === 'unsaved' && value === false) && !(Array.isArray(value) && value.length === 0))
    .sort(([a],[b]) => a.localeCompare(b)).map(([key,value]) => [key,canonical(value)]));
  return value;
}
function semanticKey(item: ChatAttachment): string {
  if (!('schema_version' in item)) return contextAttachmentKey(item);
  const rest = Object.fromEntries(Object.entries(item).filter(([key]) => key !== 'id' && key !== 'label'));
  return JSON.stringify(canonical(rest));
}

/** Three-way merge: remote owns acknowledged data; local edits are applied on
 * top. Stale unchanged caches cannot resurrect attachments sent elsewhere. */
export function mergeDraft(base: DraftValue, local: DraftValue, remote: DraftValue) {
  const baseKeys = new Set(base.attachments.map(contextAttachmentKey));
  const localKeys = new Set(local.attachments.map(contextAttachmentKey));
  const removed = new Set([...baseKeys].filter(key => !localKeys.has(key)));
  const attachments = remote.attachments.filter(item => !removed.has(contextAttachmentKey(item)));
  const known = new Set(attachments.map(semanticKey));
  for (const item of local.attachments) {
    if (!baseKeys.has(contextAttachmentKey(item)) && !known.has(semanticKey(item))) {
      attachments.push(item);
      known.add(semanticKey(item));
    }
  }
  const dirtyText = local.text !== base.text;
  return {
    value: { text: dirtyText ? local.text : remote.text, attachments },
    conflict: dirtyText && remote.text !== base.text && remote.text !== local.text,
  };
}

const empty = (chatId: string): ServerDraft => ({chat_id:chatId,version:0,generation:0,text:'',attachments:[]});

export class DraftController {
  remote: ServerDraft;
  pending?: DraftOperation;
  submission?: DraftValue & {generation:number};
  private recovering = false;
  private discardInitial = false;
  private discardOperations?: DraftOperation[];
  ready = false;
  sending = false;
  conflict = false;
  error: Error | null = null;
  private disposed = false;
  dispose() { this.disposed = true; }
  private chain: Promise<unknown> = Promise.resolve();
  readonly chatId: string;
  private transport: DraftTransport;
  private bridge: { read(): DraftValue; write(value: DraftValue): void; save(cache: DraftCache): void; changed(): void };
  constructor(chatId: string, transport: DraftTransport, bridge: DraftController['bridge'], cache?: DraftCache, discardInitial = false) {
    this.discardInitial = discardInitial;
    this.chatId = chatId;
    this.transport = transport;
    this.bridge = bridge;
    this.remote = cache?.remote ?? empty(chatId);
    this.pending = cache?.pending;
    this.submission = cache?.submission;
    this.recovering = !!cache?.submission;
  }
  save() { if (this.disposed) return; this.bridge.save({...this.bridge.read(),remote:this.remote,pending:this.pending,submission:this.submission}); }
  private queue<T>(operation: () => Promise<T>): Promise<T> {
    const next = this.chain.then(() => {if (this.disposed) throw new Error('session_changed'); return operation();}).then(value => {
      this.error = null; this.save(); this.bridge.changed(); return value;
    }, error => {
      this.error = error instanceof Error ? error : new Error(String(error));
      this.save(); this.bridge.changed(); throw error;
    });
    this.chain = next.catch(() => undefined);
    return next;
  }
  private accept(remote: ServerDraft, ack?: DraftOperation, consumed = false) {
    if (this.disposed || remote.version < this.remote.version) return;
    const local = this.bridge.read();
    // An acknowledgement can return a later generation (another window sent
    // meanwhile). Remove exactly the accepted local additions before merging.
    if (ack?.kind === 'append') {
      const keys = new Set(ack.attachments.map(contextAttachmentKey));
      local.attachments = local.attachments.filter(item => !keys.has(contextAttachmentKey(item)));
    }
    let base = consumed ? {text:'',attachments:this.remote.attachments.filter(item =>
      this.bridge.read().attachments.some(local => contextAttachmentKey(local) === contextAttachmentKey(item)))} : this.remote;
    if (ack?.kind === 'text') base = {...base,text:ack.text};
    const merged = mergeDraft(base, local, remote);
    this.remote = remote;
    this.conflict = consumed ? false : this.conflict || merged.conflict;
    this.ready = true;
    this.bridge.write(merged.value);
    this.save();
    this.bridge.changed();
  }
  private async execute(operation: DraftOperation) {
    this.pending = operation;
    // Persist the exact operation BEFORE dispatch: a lost response retries the
    // same receipt even after refresh, never a new mutation of a sent draft.
    this.save();
    let remote: ServerDraft;
    try { remote = await this.transport.mutate(operation); }
    catch (error) {
      if (error instanceof Error && error.message === 'draft_text_changed_in_another_window') {
        this.pending = undefined;
        this.accept(await this.transport.get());
        this.conflict = true;
      }
      throw error;
    }
    this.pending = undefined;
    this.accept(remote, operation);
  }
  private async fetch(accepted = false) {
    const remote = await this.transport.get();
    let consumed = false;
    if (this.recovering && this.submission) {
      consumed = accepted || remote.generation > this.submission.generation;
      if (!consumed) {
        const local = this.bridge.read();
        const keys = new Set(this.submission.attachments.map(contextAttachmentKey));
        this.bridge.write({
          text:local.text ? `${this.submission.text}\n\n${local.text}` : this.submission.text,
          attachments:[...this.submission.attachments,...local.attachments.filter(a => !keys.has(contextAttachmentKey(a)))],
        });
      }
      this.submission = undefined; this.recovering = false;
    }
    this.accept(remote,undefined,consumed);
  }
  private async discardInitialDraft() {
    if (!this.discardInitial) return;
    if (!this.discardOperations) {
      const snapshot = await this.transport.get();
      this.discardOperations = [];
      if (snapshot.attachments.length) this.discardOperations.push({kind:'remove',operation_id:crypto.randomUUID(),attachment_keys:snapshot.attachments.map(contextAttachmentKey)});
      if (snapshot.text) this.discardOperations.push({kind:'text',operation_id:crypto.randomUUID(),previous_text:snapshot.text,text:''});
    }
    while (this.discardOperations.length) {
      const operation = this.discardOperations[0];
      try { await this.transport.mutate(operation); }
      catch (error) {
        // A concurrently edited text belongs to the new draft. Never erase it.
        if (operation.kind !== 'text' || !(error instanceof Error) || error.message !== 'draft_text_changed_in_another_window') throw error;
      }
      this.discardOperations.shift();
    }
    this.discardInitial = false;
    // New typing and references arriving after the snapshot survive the merge.
    await this.fetch();
  }
  refresh() { return this.queue(async () => {
    if (this.sending) return;
    await this.discardInitialDraft();
    if (this.pending) await this.execute(this.pending);
    await this.fetch();
  }); }
  flush() { return this.queue(async () => {
    if (this.sending) return;
    await this.discardInitialDraft();
    if (this.pending) await this.execute(this.pending);
    if (!this.ready || this.recovering) await this.fetch();
    if (this.conflict) throw new Error('draft_text_changed_in_another_window');
    const desired = this.bridge.read();
    const localKeys = new Set(desired.attachments.map(contextAttachmentKey));
    const removed = this.remote.attachments.map(contextAttachmentKey).filter(key => !localKeys.has(key));
    if (removed.length) await this.execute({kind:'remove',operation_id:crypto.randomUUID(),attachment_keys:removed});
    for (const item of desired.attachments) {
      if (!this.bridge.read().attachments.some(current => contextAttachmentKey(current) === contextAttachmentKey(item))) continue;
      if (this.remote.attachments.some(current => semanticKey(current) === semanticKey(item))) continue;
      await this.execute({kind:'append',operation_id:crypto.randomUUID(),attachments:[item]});
    }
    const text = this.bridge.read().text;
    if (text !== this.remote.text) await this.execute({kind:'text',operation_id:crypto.randomUUID(),previous_text:this.remote.text,text});
  }); }
  remove(item: ChatAttachment) { return this.queue(async () => {
    // Explicit removal supersedes a failed append, including an ambiguous
    // network response. The server remove remains idempotent and authorized.
    const key = contextAttachmentKey(item);
    if (this.pending?.kind === 'append' && this.pending.attachments.some(a => contextAttachmentKey(a) === key)) this.pending = undefined;
    await this.execute({kind:'remove',operation_id:crypto.randomUUID(),attachment_keys:[key]});
  }); }
  beginSend() { this.submission = {...this.bridge.read(),generation:this.remote.generation}; this.sending = true; this.save(); }
  /** The caller already restored a purely local, unaccepted submission. */
  discardSubmission() { this.sending = false; this.submission = undefined; this.save(); }
  finishSend(accepted: boolean) { return this.queue(async () => {
    // The HTTP response can disappear after durable acceptance. Reconcile
    // against the server generation before restoring the submitted draft.
    this.sending = false;
    this.recovering = !!this.submission;
    this.ready = false;
    await this.fetch(accepted);
  }); }
  chooseText(local: boolean) {
    this.conflict = false;
    if (!local) this.bridge.write({...this.bridge.read(), text:this.remote.text});
    this.save(); this.bridge.changed();
    return this.flush();
  }
}
