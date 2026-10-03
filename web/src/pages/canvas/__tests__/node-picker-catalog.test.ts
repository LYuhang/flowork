import { describe, expect, it } from 'vitest';
import { ADDABLE_NODE_TYPES } from '../nodes/NODE_TYPES';
import { NODE_PICKER_GROUPS, NODE_SEARCH_ALIASES, availableNodePosition } from '../nodePickerCatalog';
import { nodeInsertPayload } from '../explorer/nodeCatalog';
import { useWorkflowEditStore } from '@/stores/workflow-edit';

describe('node picker catalog and insertion', () => {
  it('offers every registered addable type once with bilingual search terms', () => {
    const types = NODE_PICKER_GROUPS.flatMap((group) => group.types);
    expect(new Set(types).size).toBe(types.length);
    expect([...types].sort()).toEqual([...ADDABLE_NODE_TYPES].sort());
    for (const type of types) expect(NODE_SEARCH_ALIASES[type]).toMatch(/[\u4e00-\u9fff]/);
  });
  it.each(ADDABLE_NODE_TYPES)('inserts %s through the shared draft store, undoing as one edit', (type) => {
    const store = useWorkflowEditStore.getState();
    store.setDraft({});
    store.addNode(nodeInsertPayload(type), { x: 120, y: 150 });
    const entries = Object.values(useWorkflowEditStore.getState().draft ?? {});
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ node_type: type, __attributes__: { x: 120, y: 150 } });
    useWorkflowEditStore.getState().undo();
    expect(useWorkflowEditStore.getState().draft).toEqual({});
    useWorkflowEditStore.getState().redo();
    expect(Object.values(useWorkflowEditStore.getState().draft ?? {})).toHaveLength(1);
  });
  it('places a central addition outside an occupied node rectangle', () => {
    const center = { x: 400, y: 300 };
    const first = availableNodePosition(center, []);
    const second = availableNodePosition(center, [{ position: first, measured: { width: 240, height: 140 } }]);
    expect(second).not.toEqual(first);
    expect(Math.abs(second.x-first.x)>=260 || Math.abs(second.y-first.y)>=160).toBe(true);
  });
});
