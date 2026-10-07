import { beforeEach, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock('@/lib/api/session-fetch', () => ({ sessionFetch: mocks.fetch }));
import { readExecutionEvents } from '../workflow-history';
const frames = (from: number, to: number) => Array.from({length: to-from+1}, (_,i)=>({seq:from+i,type:'node_finished'}));
const page = (from:number,to:number,head:number) => new Response(JSON.stringify({events:frames(from,to),last_seq:head,status:'running'}));
beforeEach(()=>mocks.fetch.mockReset());
it('drains paginated history without losing previous frames or mutating cached data',async()=>{
 const previous=frames(1,2), signal=new AbortController().signal;
 mocks.fetch.mockResolvedValueOnce(page(3,502,1003)).mockResolvedValueOnce(page(503,1002,1003)).mockResolvedValueOnce(page(1003,1003,1003));
 const result=await readExecutionEvents('run',previous,signal);
 expect(result.events).toEqual(frames(1,1003)); expect(previous).toHaveLength(2);
 expect(mocks.fetch.mock.calls.map(([url])=>new URL(url,'http://test').searchParams.get('after'))).toEqual(['2','502','1002']);
 expect(mocks.fetch.mock.calls.every(([,init])=>init.signal===signal)).toBe(true);
});
it('finishes a bounded catchup even if the producer keeps extending the head',async()=>{
 mocks.fetch.mockResolvedValueOnce(page(1,500,600)).mockResolvedValueOnce(page(501,1000,1500));
 const result=await readExecutionEvents('run',[]);
 expect(result.events).toHaveLength(1000); expect(mocks.fetch).toHaveBeenCalledTimes(2);
});
it('rejects gaps and pages that cannot advance, preserving caller cache',async()=>{
 const previous=frames(1,1);
 mocks.fetch.mockResolvedValueOnce(page(3,4,4));
 await expect(readExecutionEvents('run',previous)).rejects.toThrow('sequence');
 mocks.fetch.mockResolvedValueOnce(page(1,1,4));
 await expect(readExecutionEvents('run',previous)).rejects.toThrow('missing');
 expect(previous).toEqual(frames(1,1));
});
it('does not turn a failed or aborted fetch into an empty successful history',async()=>{
 mocks.fetch.mockResolvedValueOnce(new Response('',{status:403}));
 await expect(readExecutionEvents('run',[])).rejects.toThrow('403');
 mocks.fetch.mockRejectedValueOnce(new DOMException('Aborted','AbortError'));
 await expect(readExecutionEvents('run',[])).rejects.toThrow('Aborted');
});
