import { expect, it } from 'vitest';
import { useNodeExecStore } from '@/stores/node-exec';

it('retains partial trace output alongside a node failure', () => {
  const store = useNodeExecStore;
  store.getState().begin('node_1', new AbortController(), 'workflow');
  const result = JSON.stringify({ __traces__: [{ role: 'tool', content: 'failed command' }] });
  store.getState().applyUpdate({ node_id: 'node_1', status: 'error', result, error: 'failed' });
  expect(store.getState().result).toBe(result);
  expect(store.getState().error).toBe('failed');
  store.getState().reset();
});
