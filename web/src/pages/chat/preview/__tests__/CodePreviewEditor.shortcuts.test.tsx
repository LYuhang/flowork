import { EditorView } from '@codemirror/view';
import { act, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { CodePreviewEditor } from '../CodePreviewEditor';

describe('CodePreviewEditor desktop editing contract', () => {
  it('captures the editor selection including indentation and source line coordinates', () => {
    const onSelectionChange = vi.fn();
    const value = 'first\n    second\nthird';
    render(<CodePreviewEditor value={value} language={{ id: 'text', description: null }} readOnly
      ariaLabel="selected source" onChange={() => undefined} onSelectionChange={onSelectionChange} />);
    const editor = screen.getByRole('textbox', { name: 'selected source' });
    const view = EditorView.findFromDOM(editor.closest('.cm-editor') as HTMLElement)!;
    act(() => view.dispatch({ selection: { anchor: 6, head: 17 } }));
    expect(onSelectionChange).toHaveBeenLastCalledWith({ text: '    second\n', startLine: 2, endLine: 2 });
    act(() => view.dispatch({ selection: { anchor: 0 } }));
    expect(onSelectionChange).toHaveBeenLastCalledWith(null);
  });
  it('advertises and retains standard clipboard/history shortcuts', () => {
    render(
      <CodePreviewEditor
        value={'print("hello")\n'}
        language={{ id: 'Python', description: null }}
        readOnly={false}
        ariaLabel="example.py source"
        onChange={() => undefined}
      />,
    );

    const editor = screen.getByRole('textbox', { name: 'example.py source' });
    const surface = editor.closest('[data-role="code-preview-editor"]');
    expect(surface).toHaveAttribute(
      'aria-keyshortcuts',
      'Control+C Meta+C Control+V Meta+V Control+Z Meta+Z Control+Y Meta+Shift+Z',
    );
    expect(editor).toHaveAttribute('contenteditable', 'true');
  });
});
