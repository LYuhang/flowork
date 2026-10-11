import { describe, expect, it, vi } from 'vitest';
import { createWorkbookTheme } from '../workbook-theme';

const renderWorkbook = vi.hoisted(() => vi.fn());
vi.mock('@file-viewer/renderer-spreadsheet', () => ({
  spreadsheetRenderer: {
    handlers: [{ rendererId: 'spreadsheet-openxml', handler: renderWorkbook }],
  },
}));

describe('workbook theme adapter', () => {
  it('applies the latest theme when loading finishes without rendering again', async () => {
    const instance = { setTheme: vi.fn(), unmount: vi.fn() };
    let finish!: (value: typeof instance) => void;
    renderWorkbook.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const adapter = createWorkbookTheme('light');
    const mounting = adapter.renderer.handlers![0].handler(new ArrayBuffer(0), document.createElement('div'));
    adapter.setTheme('dark');
    finish(instance);
    const mounted = await mounting;
    expect(instance.setTheme).toHaveBeenLastCalledWith('dark');
    adapter.setTheme('light');
    expect(instance.setTheme).toHaveBeenLastCalledWith('light');
    expect(renderWorkbook).toHaveBeenCalledTimes(1);
    mounted.unmount();
    instance.setTheme.mockClear();
    adapter.setTheme('dark');
    expect(instance.setTheme).not.toHaveBeenCalled();
    expect(instance.unmount).toHaveBeenCalledTimes(1);
    const nextFile = { setTheme: vi.fn(), unmount: vi.fn() };
    renderWorkbook.mockResolvedValueOnce(nextFile);
    const next = await adapter.renderer.handlers![0].handler(new ArrayBuffer(0), document.createElement('div'));
    expect(nextFile.setTheme).toHaveBeenLastCalledWith('dark');
    adapter.setTheme('light');
    expect(nextFile.setTheme).toHaveBeenLastCalledWith('light');
    expect(instance.setTheme).not.toHaveBeenCalled();
    next.unmount();
  });
});
