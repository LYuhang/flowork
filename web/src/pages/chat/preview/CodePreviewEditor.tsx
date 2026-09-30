import { textSelectionFromOffsets, type PreviewTextSelection } from '@/lib/preview/text-reference';
import { useCallback, useMemo, type KeyboardEvent } from 'react';
import CodeMirror from '@uiw/react-codemirror';
import { indentWithTab } from '@codemirror/commands';
import { indentUnit } from '@codemirror/language';
import { EditorState, type Extension } from '@codemirror/state';
import { EditorView, keymap } from '@codemirror/view';
import { useTheme } from 'next-themes';
import { useEffect, useState } from 'react';

import type { CodePreviewLanguage } from './code-preview-language';

const BASE_EXTENSIONS: Extension[] = [
  keymap.of([indentWithTab]),
  EditorState.tabSize.of(4),
  EditorView.lineWrapping,
];

export interface CodePreviewEditorProps {
  value: string;
  language: CodePreviewLanguage;
  readOnly: boolean;
  ariaLabel: string;
  onChange: (next: string) => void;
  onSelectionChange?: (selection: PreviewTextSelection | null) => void;
}

/** Professional code surface shared by every file opened through Preview. */
export function CodePreviewEditor({
  value,
  language,
  readOnly,
  ariaLabel,
  onChange,
  onSelectionChange,
}: CodePreviewEditorProps) {
  const { resolvedTheme } = useTheme();
  const [loadedLanguage, setLoadedLanguage] = useState<{
    id: string;
    extension: Extension;
  } | null>(null);
  useEffect(() => {
    let active = true;
    if (language.description && !language.description.support) {
      void language.description.load().then((support) => {
        if (active) setLoadedLanguage({ id: language.id, extension: support });
      });
    }
    return () => {
      active = false;
    };
  }, [language]);
  const languageExtension = language.description?.support
    ?? (loadedLanguage?.id === language.id ? loadedLanguage.extension : null);
  const extensions = useMemo(() => [
    ...BASE_EXTENSIONS,
    indentUnit.of(language.id.toLowerCase() === 'python' ? '    ' : '  '),
    EditorView.contentAttributes.of({ 'aria-label': ariaLabel }),
    ...(languageExtension ? [languageExtension] : []),
  ], [ariaLabel, language.id, languageExtension]);
  const basicSetup = useMemo(
    () => ({
      lineNumbers: true,
      foldGutter: true,
      history: true,
      defaultKeymap: true,
      historyKeymap: true,
      highlightActiveLine: !readOnly,
      highlightActiveLineGutter: !readOnly,
    }),
    [readOnly],
  );
  const handleChange = useCallback((next: string) => onChange(next), [onChange]);
  const stopKeyPropagation = useCallback((event: KeyboardEvent<HTMLDivElement>) => {
    event.stopPropagation();
  }, []);

  return (
    <div
      className="h-full min-h-0 overflow-hidden bg-background text-xs [&_.cm-content]:min-h-full [&_.cm-editor]:h-full [&_.cm-focused]:outline-none [&_.cm-gutters]:border-r [&_.cm-gutters]:border-edge-subtle [&_.cm-gutters]:bg-muted/30 [&_.cm-line]:font-mono [&_.cm-line]:leading-5 [&_.cm-scroller]:font-mono"
      onKeyDown={stopKeyPropagation}
      data-role="code-preview-editor"
      aria-keyshortcuts="Control+C Meta+C Control+V Meta+V Control+Z Meta+Z Control+Y Meta+Shift+Z"
    >
      <CodeMirror
        className="h-full min-h-0"
        value={value}
        editable={!readOnly}
        readOnly={readOnly}
        aria-label={ariaLabel}
        onChange={handleChange}
        onUpdate={update => {
          if (!onSelectionChange || (!update.selectionSet && !update.docChanged)) return;
          const { from, to } = update.state.selection.main;
          onSelectionChange(textSelectionFromOffsets(update.state.doc.toString(), from, to));
        }}
        extensions={extensions}
        basicSetup={basicSetup}
        height="100%"
        theme={resolvedTheme === 'dark' ? 'dark' : 'light'}
      />
    </div>
  );
}
