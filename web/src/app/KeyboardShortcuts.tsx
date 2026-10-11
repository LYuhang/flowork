/**
 * Global keyboard shortcut surface.
 *
 * Renders nothing; mounts once in {@link AppLayout} and attaches a
 * window-level keydown listener for the cross-cutting shortcuts:
 *
 *   - Cmd/Ctrl+K          → toggle the command palette
 *   - Cmd/Ctrl+S          → save the active workflow (clicks the toolbar
 *                           save button so we inherit its disabled state
 *                           and side-effects without duplicating logic)
 *   - Cmd/Ctrl+Z          → workflow undo (skipped when focus is in a
 *                           text input — the user is editing text, not
 *                           the graph)
 *   - Cmd/Ctrl+Shift+Z    → workflow redo (same focus carve-out)
 *   - Delete / Backspace  → delete the selected node / edge from the
 *                           draft (Stream 4). Skipped while editing text
 *                           (so Backspace in a field edits the field) and
 *                           when the canvas is read-only.
 *   - Cmd/Ctrl+C          → copy the selected node into the in-memory
 *                           node clipboard (Stream 4).
 *   - Cmd/Ctrl+V          → paste the clipboard near the selected node
 *                           (or at +24/+24) as a fresh, disconnected node.
 *   - Cmd/Ctrl+D          → duplicate the selected node (copy + paste in
 *                           one undo step).
 *   - Escape              → cascade (T17):
 *                           1. command palette open  → close it
 *                           2. else any Radix dialog open  → close
 *                              the topmost one (last-opened in DOM order)
 *                           3. else defer to canvas (no-op today; T18+
 *                              can wire deselect-nodes here)
 *
 * We treat `metaKey || ctrlKey` as the same primary modifier so macOS
 * and Linux/Windows users get the same shortcuts without per-platform
 * branching here. The displayed labels ("⌘S" etc.) live next to each
 * `CommandItem` and can be localised in T18.
 */
import { useEffect } from 'react';
import { useUIStore } from '@/stores/ui';
import { readSelectedEdge, readSelectedNodeId } from './keyboard-shortcut-selection';
import { useWorkflowEditStore } from '@/stores/workflow-edit';

/** Element types where the user is editing text, not the graph. */
function isTextEditingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (target.isContentEditable) return true;
  return false;
}

/**
 * True when the user has an actual (non-collapsed, non-empty) text selection
 * anywhere on the page — e.g. they dragged across text in the inspector sider.
 *
 * Cmd/Ctrl+C is overloaded: it copies the SELECTED CANVAS NODE into the node
 * clipboard. But a node stays selected while you read its sider, so a naïve
 * handler hijacks Cmd+C and `preventDefault()`s the browser's native text copy
 * — selecting sider text then pressing Cmd+C copied the node, not the text.
 * When real text is selected we defer to the browser (text copy wins); node
 * copy only fires when nothing is selected.
 */
function hasTextSelection(): boolean {
  const sel = typeof window !== 'undefined' ? window.getSelection() : null;
  return !!sel && !sel.isCollapsed && sel.toString().trim().length > 0;
}

/**
 * Read the currently-selected canvas node/edge id from the xyflow DOM.
 *
 * Why the DOM and not the store: selection lives in xyflow's internal
 * node/edge state, which is only reachable through `useReactFlow()` /
 * `useNodes()` *inside* the `ReactFlowProvider`. This window-level handler
 * is mounted globally in `AppLayout`, outside that provider, so it cannot
 * subscribe to the hook. xyflow renders each node as
 * `.react-flow__node[data-id]` and each edge as `.react-flow__edge[data-id]`,
 * adding a `selected` class when selected (a stable part of xyflow's
 * documented DOM contract — they query the same selector internally). The
 * edge `data-id` is `${source}->${target}` (see `workflowDictToNodesEdges`).
 *
 * Exported for unit tests, which seed the matching DOM nodes directly.
 */
/**
 * Flow-coord anchor for a keyboard paste. We prefer the selected node's own
 * stored position offset by (+24,+24) so a duplicate lands just off its
 * source; falling back to (24,24) when nothing is selected. (Reading the
 * live viewport center would need the canvas's `screenToFlowPosition`, which
 * is context-bound and unreachable from this global handler — the spec's
 * +24/+24 fallback.)
 */
function pasteAnchor(selectedId: string | null): { x: number; y: number } {
  const draft = useWorkflowEditStore.getState().draft;
  if (selectedId && draft) {
    const node = draft[selectedId];
    if (node && typeof node === 'object') {
      const attrs = (node as Record<string, unknown>).__attributes__ as
        | Record<string, unknown>
        | undefined;
      const x = typeof attrs?.x === 'number' ? attrs.x : 0;
      const y = typeof attrs?.y === 'number' ? attrs.y : 0;
      return { x: x + 24, y: y + 24 };
    }
  }
  return { x: 24, y: 24 };
}

/**
 * Find the close affordance inside an open Radix dialog and click it.
 *
 * Why this dance: `src/components/ui/dialog.tsx` renders the auto-close X
 * as `<DialogPrimitive.Close><X /><span className="sr-only">Close</span>`,
 * with NO `aria-label` on the button itself (Radix gives it an accessible
 * name via the sr-only child instead). So `[aria-label="Close"]` — the
 * naïve selector — would match nothing in this codebase.
 *
 * Robust selector: walk all `button`s inside the dialog and pick the one
 * whose descendant text equals "Close" via an `sr-only` span. This is
 * stable across every modal in `src/components/modals/*` because they
 * all use the shared `DialogContent` primitive.
 *
 * Returns `true` if a dialog was found and a close was dispatched.
 */
function closeTopmostDialog(): boolean {
  const open = document.querySelectorAll<HTMLElement>(
    '[role="dialog"][data-state="open"]',
  );
  if (open.length === 0) return false;
  // Radix portals each open dialog as the most recently appended subtree,
  // so the last match in DOM order is the topmost on screen — the one
  // the user expects Escape to close first.
  const top = open[open.length - 1];
  // The auto-rendered Close button is the one whose sr-only label reads
  // "Close". Any user-authored Cancel/Close buttons in the footer can
  // also match, but Radix's primitive close is always present and is the
  // first to render in DOM order, so we accept the first hit.
  const buttons = top.querySelectorAll<HTMLButtonElement>('button');
  for (const btn of buttons) {
    const srOnly = btn.querySelector<HTMLElement>('.sr-only');
    if (srOnly && srOnly.textContent?.trim() === 'Close') {
      btn.click();
      return true;
    }
  }
  // Fallback: if the dialog has no sr-only close (some custom dialogs
  // suppress it), dispatch a synthetic Escape on the content node so
  // Radix's own keydown listener can pick it up. This is a defence-in-
  // depth path — we still report success so the cascade halts here.
  top.dispatchEvent(
    new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
  );
  return true;
}

export function KeyboardShortcuts() {
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.isComposing || e.keyCode === 229) return;
      const mod = e.metaKey || e.ctrlKey;
      const key = e.key.toLowerCase();

      // Cmd/Ctrl+K — toggle palette. Always wins, even from inputs.
      if (mod && key === 'k') {
        e.preventDefault();
        const { commandPaletteOpen, setCommandPaletteOpen } =
          useUIStore.getState();
        setCommandPaletteOpen(!commandPaletteOpen);
        return;
      }

      // Cmd/Ctrl+S — save the active canvas workflow. Global; fine to
      // intercept from inputs too, since the browser's "save page"
      // dialog isn't useful here. We target the canvas-scoped attribute
      // (`canvas-save`) rather than a generic `save`, so future modals with
      // their own Save buttons cannot accidentally win the query race.
      if (mod && !e.shiftKey && key === 's') {
        e.preventDefault();
        document
          .querySelector<HTMLButtonElement>('[data-action="canvas-save"]')
          ?.click();
        return;
      }

      // Cmd/Ctrl+Shift+Z — workflow redo. Skip when typing in inputs so
      // the browser's native redo still works for text fields, and when the
      // active canvas is read-only (pinned version — Stream 0d) so a global
      // keypress can't mutate a view-only draft.
      if (mod && e.shiftKey && key === 'z') {
        if (isTextEditingTarget(e.target)) return;
        if (useUIStore.getState().canvasReadOnly) return;
        e.preventDefault();
        useWorkflowEditStore.getState().redo();
        return;
      }

      // Cmd/Ctrl+Z — workflow undo. Same input + read-only carve-outs.
      if (mod && !e.shiftKey && key === 'z') {
        if (isTextEditingTarget(e.target)) return;
        if (useUIStore.getState().canvasReadOnly) return;
        e.preventDefault();
        useWorkflowEditStore.getState().undo();
        return;
      }

      // The clipboard / delete shortcuts below all mutate the draft, so
      // they share the same two carve-outs as undo/redo: don't fire while
      // editing text (Backspace/Cmd+C must edit the FIELD, not the graph)
      // and never on a pinned read-only canvas (Stream 4 guards F11/gap#4).
      const editingText = isTextEditingTarget(e.target);
      const readOnly = useUIStore.getState().canvasReadOnly;

      // Cmd/Ctrl+C — copy the selected node into the node clipboard.
      if (mod && !e.shiftKey && key === 'c') {
        if (editingText || readOnly) return;
        // Defer to the browser's native copy when text is selected (e.g. the
        // user dragged across sider text) — node-copy only with no selection.
        if (hasTextSelection()) return;
        const id = readSelectedNodeId();
        if (!id) return;
        e.preventDefault();
        useWorkflowEditStore.getState().copyNodes([id]);
        return;
      }

      // Cmd/Ctrl+V — paste the clipboard near the selected node.
      if (mod && !e.shiftKey && key === 'v') {
        if (editingText || readOnly) return;
        const { clipboard, pasteNodes } = useWorkflowEditStore.getState();
        if (clipboard.length === 0) return;
        e.preventDefault();
        pasteNodes(pasteAnchor(readSelectedNodeId()));
        return;
      }

      // Cmd/Ctrl+D — duplicate the selected node (copy + paste, one undo
      // step from the user's view — paste is a single `applyEdit`).
      if (mod && !e.shiftKey && key === 'd') {
        if (editingText || readOnly) return;
        const id = readSelectedNodeId();
        if (!id) return;
        e.preventDefault();
        const store = useWorkflowEditStore.getState();
        store.copyNodes([id]);
        store.pasteNodes(pasteAnchor(id));
        return;
      }

      // Delete / Backspace — remove the selected node, or the selected
      // edge if no node is selected, from the draft. The canvas itself
      // also handles Delete natively (xyflow `onNodesDelete` /
      // edge-remove) when it has focus; this global path covers the case
      // where focus is elsewhere on the page. `removeNode` /
      // `disconnectNodes` are idempotent so a double-fire is harmless.
      if (key === 'delete' || key === 'backspace') {
        if (editingText || readOnly) return;
        const nodeId = readSelectedNodeId();
        if (nodeId) {
          e.preventDefault();
          useWorkflowEditStore.getState().removeNode(nodeId);
          return;
        }
        const edge = readSelectedEdge();
        if (edge) {
          e.preventDefault();
          useWorkflowEditStore.getState().disconnectNodes(edge.source, edge.target);
          return;
        }
        return;
      }

      // Escape — cascade: palette first, then topmost open dialog, then
      // defer to the canvas. We `preventDefault()` only when we actually
      // handle the key so dialogs we didn't close (e.g. a Radix
      // `Popover` that owns its own Escape) keep working. Escape is
      // global — no `isTextEditingTarget` carve-out — because users
      // expect Esc to dismiss UI even while typing.
      if (key === 'escape') {
        const { commandPaletteOpen, setCommandPaletteOpen } =
          useUIStore.getState();
        if (commandPaletteOpen) {
          e.preventDefault();
          setCommandPaletteOpen(false);
          return;
        }
        if (closeTopmostDialog()) {
          e.preventDefault();
          return;
        }
        // Step 3: defer to canvas — no-op today. T18+ can hook deselect
        // here without touching the cascade structure.
      }
    };

    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  return null;
}
