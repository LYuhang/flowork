import { afterEach, describe, expect, it, vi } from 'vitest';
import { Position, type InternalNode } from '@xyflow/react';
const mocks = vi.hoisted(() => ({ calculate: vi.fn(), terminate: vi.fn() }));
vi.mock('elkjs/lib/elk-worker.min.js?url', () => ({ default: '/assets/layout-worker.js' }));
vi.mock('elkjs/lib/elk-api', () => ({ default: class { terminateWorker = mocks.terminate; } }));
vi.mock('../compact-layout', () => ({ calculateCompactLayout: mocks.calculate }));
import { measuredLayoutInput, runCompactLayout } from '../run-compact-layout';
afterEach(() => { vi.clearAllMocks(); vi.useRealTimers(); });
const nodes = [{ id: 'a', measured: { width: 224, height: 360 }, internals: { handleBounds: {
  source: [{ id: null, x: 221, y: 47, width: 6, height: 6, position: Position.Right }], target: [],
} } }] as unknown as InternalNode[];
const viewport = { width: 1000, height: 600 };
describe('layout worker lifecycle', () => {
  it('uses the actual handle boundary, not the expanded card center', () => {
    expect(measuredLayoutInput(nodes, []).nodes[0].ports[0]).toEqual({ id: 'source:', x: 227, y: 50, side: 'EAST' });
  });
  it('terminates the worker after success', async () => {
    mocks.calculate.mockResolvedValue({ width: 100 });
    expect(await runCompactLayout(nodes, [], viewport, new AbortController().signal)).toEqual({ width: 100 });
    expect(mocks.terminate).toHaveBeenCalledTimes(1);
  });
  it('terminates on a calculation error without retrying', async () => {
    mocks.calculate.mockRejectedValue(Error('failed'));
    await expect(runCompactLayout(nodes, [], viewport, new AbortController().signal)).rejects.toThrow('failed');
    expect(mocks.calculate).toHaveBeenCalledTimes(1);
    expect(mocks.terminate).toHaveBeenCalledTimes(1);
  });
  it('terminates a pending calculation when leaving the canvas', async () => {
    mocks.calculate.mockReturnValue(new Promise(() => {}));
    const controller = new AbortController();
    const promise = runCompactLayout(nodes, [], viewport, controller.signal);
    controller.abort();
    await expect(promise).rejects.toMatchObject({ name: 'AbortError' });
    expect(mocks.terminate).toHaveBeenCalledTimes(1);
  });
  it('bounds a stalled worker and reports the timeout', async () => {
    vi.useFakeTimers();
    mocks.calculate.mockReturnValue(new Promise(() => {}));
    const promise = runCompactLayout(nodes, [], viewport, new AbortController().signal);
    const assertion = expect(promise).rejects.toThrow('30 seconds');
    await vi.advanceTimersByTimeAsync(30_000);
    await assertion;
    expect(mocks.terminate).toHaveBeenCalledTimes(1);
  });
});
