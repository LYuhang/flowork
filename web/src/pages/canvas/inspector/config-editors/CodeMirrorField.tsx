/**
 * Shared CodeMirror 6 editor for node-config text bodies.
 *
 * Node config editors use a professional code editor instead of a plain
 * <textarea>:
 *   - CodeNode `process_fn` — Python syntax highlighting + line numbers.
 *   - PromptNode `prompt_template` — {{placeholder}} highlighting.
 *   - SubAgentNode `task_template` — the same prompt-authoring surface.
 *
 * ## Commit semantics (matches CommitOnBlurTextarea)
 *
 * The previous textareas committed ON BLUR so the undo stack doesn't get a
 * new entry per keystroke. We preserve that: the editor keeps a LOCAL buffer
 * while focused and only calls `onCommit` when it loses focus (and only if the
 * value actually changed). Upstream `value` changes (undo/redo, node switch,
 * agent edits) resync the buffer via the prev-prop-in-state pattern.
 *
 * `@uiw/react-codemirror` is a controlled wrapper; we feed it the LOCAL buffer
 * so typing is smooth and the commit-on-blur contract is honoured.
 */
import { useState, useMemo } from 'react';
import type { KeyboardEvent } from 'react';
import CodeMirror from '@uiw/react-codemirror';
import { useTheme } from 'next-themes';
import type { Extension } from '@codemirror/state';
import { keymap, EditorView } from '@codemirror/view';
import { indentWithTab } from '@codemirror/commands';
import { cn } from '@/lib/utils';

export interface CodeMirrorFieldProps {
  value: string;
  /** Called when the editor loses focus AND the value changed. */
  onCommit: (next: string) => void;
  /** Also commit every text change. Used by large modal editors that must sync live. */
  commitOnChange?: boolean;
  readOnly?: boolean;
  /** Extra CodeMirror extensions (language, decorations, themes). */
  extensions?: Extension[];
  /** Min height of the editor surface, e.g. "200px". */
  minHeight?: string;
  /** Max height before the editor scrolls instead of stretching the sider. */
  maxHeight?: string;
  placeholder?: string;
  className?: string;
  'data-testid'?: string;
}

/** Tab inserts indentation (does NOT move focus out of the editor). */
const tabKeymap: Extension = keymap.of([indentWithTab]);

export function CodeMirrorField({
  value,
  onCommit,
  commitOnChange,
  readOnly,
  extensions,
  minHeight = '160px',
  maxHeight = '320px',
  placeholder,
  className,
  'data-testid': testId,
}: CodeMirrorFieldProps) {
  const { resolvedTheme } = useTheme();
  // Local buffer + prev-prop-in-state resync (mirrors CommitOnBlur*).
  const [local, setLocal] = useState(value);
  const [prevValue, setPrevValue] = useState(value);
  if (prevValue !== value) {
    setPrevValue(value);
    setLocal(value);
  }

  const exts = useMemo<Extension[]>(
    () => [tabKeymap, EditorView.lineWrapping, ...(extensions ?? [])],
    [extensions],
  );

  // `@uiw/react-codemirror` reconfigures the live editor whenever `onChange`,
  // `basicSetup`, or `extensions` change IDENTITY (its setup effect deps). A
  // fresh inline `onChange` closure / `basicSetup` object every render makes it
  // reconfigure on EVERY render — including the re-render the keystroke itself
  // triggers — which (with the Python language's input handlers) manifested as
  // a stray extra newline on Enter. Keep both stable.
  const basicSetup = useMemo(
    () => ({
      lineNumbers: true,
      foldGutter: false,
      highlightActiveLine: !readOnly,
      highlightActiveLineGutter: !readOnly,
    }),
    [readOnly],
  );

  const commit = () => {
    if (local !== value) onCommit(local);
  };

  const handleChange = (next: string) => {
    setLocal(next);
    if (commitOnChange && next !== value) onCommit(next);
  };

  // Stop key events bubbling to canvas-level handlers (delete/copy/paste
  // shortcuts) while editing code.
  //
  // This MUST be the BUBBLE phase (`onKeyDown`), not capture
  // (`onKeyDownCapture`). CodeMirror handles keys via a native listener on its
  // content DOM; stopping propagation in the CAPTURE phase — before CodeMirror
  // sees the key — makes React's event system re-dispatch the keydown, so a
  // single Enter inserted TWO newlines (reproduced in a real browser). In the
  // bubble phase CodeMirror processes the key first, then we stop it from
  // reaching the canvas. Same shortcut-shielding, no double input.
  const stopBubble = (e: KeyboardEvent<HTMLDivElement>) => {
    e.stopPropagation();
  };

  return (
    <div
      data-testid={testId}
      onKeyDown={stopBubble}
      className={cn(
        'overflow-hidden rounded-md border border-input bg-background text-xs focus-within:ring-1 focus-within:ring-ring',
        readOnly && 'opacity-60',
        className,
      )}
    >
      <CodeMirror
        theme={resolvedTheme === 'dark' ? 'dark' : 'light'}
        value={local}
        editable={!readOnly}
        readOnly={readOnly}
        onChange={handleChange}
        onBlur={commit}
        extensions={exts}
        placeholder={placeholder}
        minHeight={minHeight}
        maxHeight={maxHeight}
        basicSetup={basicSetup}
      />
    </div>
  );
}
