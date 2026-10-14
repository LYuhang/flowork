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
  it('reads only the workbook containing the original event and drops unmounted sources', async () => {
    const root = document.createElement('div');
    const reference = { sheet: 'Orders', firstRow: 2, lastRow: 4, firstColumn: 1, lastColumn: 3 };
    const instance = { $el: root, setTheme: vi.fn(), unmount: vi.fn(), getReference: vi.fn(() => reference) };
    renderWorkbook.mockResolvedValueOnce(instance);
    const adapter = createWorkbookTheme('light');
    const mounted = await adapter.renderer.handlers![0].handler(new ArrayBuffer(0), root);
    const event = new MouseEvent('contextmenu');
    vi.spyOn(event, 'composedPath').mockReturnValue([root]);
    expect(adapter.getReference(event)).toEqual(reference);
    expect(adapter.getReference(new MouseEvent('contextmenu'))).toBeNull();
    mounted.unmount();
    expect(adapter.getReference(event)).toBeNull();
  });

});
