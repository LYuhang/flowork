# Spreadsheet theme patch

`@file-viewer/renderer-spreadsheet@2.3.2.patch` adds an instance-level
`setTheme('light' | 'dark')` method and its public type. It updates the existing
table configuration and redraws it without loading the workbook again.

It also corrects the renderer's Shadow DOM event adapter: a cell click retargeted
to the shadow host must not be emitted as `outsideMousedown`, which otherwise
clears a selection on a second click. Actual outside clicks and window blur still
reach the table's existing handler.

`NativeWorkbookPreview` uses `workbook-theme.ts` to call this method instead of
changing FileViewer's options: core 2.3.0 reloads the source on every options
update, losing the active sheet and other viewer state. The adapter also applies
the latest theme when an asynchronous renderer finishes mounting and unregisters
instances when they unmount. It does not restore state by querying the DOM.

The patch is pinned in `package.json` and `pnpm-lock.yaml`. Docker copies patches
before its frozen dependency install. When upgrading the viewer, recheck upstream
theme handling and remove this patch/adapter if the supported API preserves state.
Verify selected sheet, zoom, scroll and selection across theme changes; a successful
reload with restored zoom alone is not sufficient.
