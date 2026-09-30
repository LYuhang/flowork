import { describe, expect, it } from 'vitest';
import { attachmentSourceHref } from './attachment-source';
import { filePageReference } from './file-reference';
import type { PreviewDescriptorV1 } from './protocol';

describe('attachment source coordinates', () => {
  it('retains the exact background task identity', () => {
    const href = attachmentSourceHref({schema_version:1,id:'j',type:'resource',label:'task',
      resource:{kind:'job',chat_id:'source',job_id:'job:1&other=2'}})!;
    const url = new URL(href, 'https://example.com');
    expect(url.pathname).toBe('/chat/open/source');
    expect(url.searchParams.get('focusJob')).toBe('job:1&other=2');
    expect(url.searchParams.has('other')).toBe(false);
  });
  it('keeps the quoted page, source revision and origin chat in the source link', () => {
    const descriptor: PreviewDescriptorV1 = {schemaVersion:1,name:'report.pdf',revision:'sha256:abc',
      sizeBytes:1,contentType:'application/pdf',detectedType:'pdf',renderer:'pdf',loadPolicy:'range',
      capabilities:{preview:true,edit:false,download:true},
      fileRef:{schemaVersion:1,scope:'project',projectId:'p',path:'/data/report.pdf'},
    };
    const attachment = filePageReference(descriptor, 2);
    const url=new URL(attachmentSourceHref(attachment,{chatId:'origin'})!, 'https://example.com');
    expect(url.searchParams.get('page')).toBe('2');
    expect(url.searchParams.get('expectedRevision')).toBe('sha256:abc');
    expect(url.searchParams.get('originChatId')).toBe('origin');
  });
  it('encodes the exact message identity without changing the source chat', () => {
    const href=attachmentSourceHref({schema_version:1,id:'q',type:'quote',label:'excerpt',
      source:{kind:'message',chat_id:'source',message_id:'message:1&other=2'},snapshot:{text:'text'}})!;
    const url=new URL(href, 'https://example.com');
    expect(url.pathname).toBe('/chat/open/source');
    expect(url.searchParams.get('focusMessage')).toBe('message:1&other=2');
    expect(url.searchParams.has('other')).toBe(false);
  });
});
