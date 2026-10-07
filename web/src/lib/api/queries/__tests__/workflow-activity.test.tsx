import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
const mocks=vi.hoisted(()=>({get:vi.fn(),watch:vi.fn()}));
vi.mock('@/lib/api/client',()=>({apiClient:{GET:mocks.get}}));
vi.mock('@/lib/api/sse/resource-activity',async importOriginal=>({...await importOriginal<typeof import('@/lib/api/sse/resource-activity')>(),watchResourceActivity:mocks.watch}));
import { useWorkflowHead,useWorkflowVersions } from '../workflow';
afterEach(()=>{vi.useRealTimers();vi.clearAllMocks();});
it('refreshes head and tree on changes without invalidating pinned graph snapshots',async()=>{
 const client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 let sub=0;const stop=vi.fn();mocks.watch.mockReturnValue(stop);
 mocks.get.mockImplementation(async(path:string)=>({data:path.endsWith('/head')?{major:1,sub,tree_revision:sub}: {items:[sub]}}));
 const pinned=['workflow-at','wf',1,0];client.setQueryData(pinned,{graph:'immutable'});
 const {result,unmount}=renderHook(()=>({head:useWorkflowHead('wf',1),tree:useWorkflowVersions('wf')}),{wrapper:({children})=><QueryClientProvider client={client}>{children}</QueryClientProvider>});
 await waitFor(()=>expect(result.current.head.data?.sub).toBe(0));
 expect(mocks.watch.mock.calls[0][0]).toBe('/api/v1/workflows/wf/activity');
 const before=mocks.get.mock.calls.length;vi.useFakeTimers();await act(async()=>{await vi.advanceTimersByTimeAsync(6000);});expect(mocks.get).toHaveBeenCalledTimes(before);vi.useRealTimers();
 sub=1;await act(async()=>{await mocks.watch.mock.calls[0][1]();});
 await waitFor(()=>expect(result.current.head.data?.sub).toBe(1));
 expect(client.getQueryData(pinned)).toEqual({graph:'immutable'});expect(client.getQueryState(pinned)?.isInvalidated).toBe(false);
 expect(client.getQueryData(['workflow-versions','wf'])).toEqual({items:[1]});unmount();expect(stop).toHaveBeenCalledTimes(1);client.clear();
});
it('does not open a stream for a disabled or absent workflow',()=>{
 const client=new QueryClient();const {unmount}=renderHook(()=>useWorkflowHead('',null,false),{wrapper:({children})=><QueryClientProvider client={client}>{children}</QueryClientProvider>});
 expect(mocks.watch).not.toHaveBeenCalled();unmount();client.clear();
});
