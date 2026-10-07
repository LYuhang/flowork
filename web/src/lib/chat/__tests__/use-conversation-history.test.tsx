import { act, renderHook } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { useConversationHistory } from '../use-conversation-history';
import type { ChatHistoryPage } from '@/lib/api/queries/chats';
const state = vi.hoisted(() => ({ page: undefined as ChatHistoryPage | undefined, older: undefined as ChatHistoryPage | undefined }));
vi.mock('@/lib/api/queries/chats', () => ({
 CHAT_INITIAL_HISTORY_LIMIT: 30,
 useChatHistory: () => ({ data: state.page }),
 fetchChatHistoryPage: async () => state.older,
}));
function page(start: number, end: number, total: number): ChatHistoryPage {
 return { items: Array.from({ length: end - start + 1 }, (_, n) => ({ id: String(start+n), role: 'assistant', content: String(start+n) })), offset: start-1, total, limit: end-start+1 };
}
beforeEach(() => { state.page = undefined; state.older = undefined; });
it('keeps visible history while the new Turn history query is pending', () => {
 state.page = page(31,60,60);
 const { result, rerender } = renderHook(() => useConversationHistory('scope','chat',true));
 state.page = undefined; rerender();
 expect(result.current.items).toHaveLength(30);
 expect(result.current.items?.[0].id).toBe('31');
 state.page = page(31,60,60); rerender();
 expect(result.current.items).toHaveLength(30);
});
it('retains the previous tail when a new response refreshes recent history', () => {
 state.page = page(31,60,60);
 const { result, rerender } = renderHook(() => useConversationHistory('scope','chat',true));
 state.page = page(41,70,70); rerender();
 expect(result.current.items).toHaveLength(40);
 expect(result.current.items?.[0].id).toBe('31');
});
it('keeps a contiguous history after earlier pages were loaded', async () => {
 state.page = page(31,60,60); state.older = page(1,30,60);
 const { result, rerender } = renderHook(() => useConversationHistory('scope','chat2',true));
 await act(async () => { await result.current.loadOlder(); });
 expect(result.current.items).toHaveLength(60);
 state.page = page(41,70,70); rerender();
 expect(result.current.items?.map(m => m.id)).toEqual(Array.from({length:70}, (_,i)=>String(i+1)));
 expect(result.current.hasOlder).toBe(false);
});
it('does not borrow retained history from another Chat or workspace', () => {
 state.page = page(1,10,10);
 const { result, rerender } = renderHook(({scope,chat}) => useConversationHistory(scope,chat,true), {initialProps:{scope:'s1',chat:'a'}});
 state.page = undefined;
 rerender({scope:'s1',chat:'b'});
 expect(result.current.items).toBeUndefined();
 rerender({scope:'s2',chat:'a'});
 expect(result.current.items).toBeUndefined();
 rerender({scope:'s1',chat:'a'});
 expect(result.current.items).toHaveLength(10);
});

it('offers older history after the first long Turn and loads it to the beginning', async () => {
 state.page = { items: [], total: 0, offset: 0, limit: 30 };
 const { result, rerender } = renderHook(() => useConversationHistory('browser-scope','long-turn',true));
 state.page = page(32,61,61); rerender();
 expect(result.current.hasOlder).toBe(true);
 state.older = page(2,31,61);
 await act(async () => { await result.current.loadOlder(); });
 expect(result.current.hasOlder).toBe(true);
 state.older = page(1,1,61);
 await act(async () => { await result.current.loadOlder(); });
 expect(result.current.hasOlder).toBe(false);
 expect(result.current.items?.map(m=>m.id)).toEqual(Array.from({length:61},(_,i)=>String(i+1)));
});

it('keeps a pagination entry for a gap after a long background response', async () => {
 state.page = page(1,30,30);
 const { result, rerender } = renderHook(() => useConversationHistory('scope','gap',true));
 state.page = page(71,100,100); rerender();
 expect(result.current.items).toHaveLength(60);
 expect(result.current.hasOlder).toBe(true);
 state.older = page(41,70,100);
 await act(async () => { await result.current.loadOlder(); });
 expect(result.current.hasOlder).toBe(true);
 state.older = page(11,40,100);
 await act(async () => { await result.current.loadOlder(); });
 expect(result.current.hasOlder).toBe(false);
 expect(result.current.items?.map(m=>m.id)).toEqual(Array.from({length:100},(_,i)=>String(i+1)));
});
