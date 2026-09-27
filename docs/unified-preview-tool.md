# Unified preview tool

The platform `interactive` MCP advertises one preview tool, `render_preview`,
and a separate waiting input tool, `render_choices`:

```json
{"type":"file","source":"/data/report.pdf","title":"Report"}
```

```json
{"type":"url","source":"https://example.com/docs","title":"Documentation"}
```

Both use flat parameters: `type`, `source`, optional `title`, `description`,
`file_type` (default `auto`, file only). Preview never waits for confirmation.
File paths must be absolute without traversal; URL sources must be
HTTP(S). Generated HTML is saved to a file and published using `type="file"`.

Main chat cards open files and URLs in the existing right-hand Preview pane.
On reload, the active URL artifact is recovered from chat history even when
other file tabs were persisted. Artifact contents are not copied into local
storage; existing file tabs remain available.
File and workflow navigation differ by surface:

- Main application: conversation card → Preview pane → maximize to a standalone Preview tab.
- Browser extension: compact conversation card → standalone Preview tab directly; no narrow inline workflow canvas.
- Workflow standalone links use `/preview?type=workflow&workflowId=…&version=vN.svM`
  under the configured deployment base path. Both version numbers are required;
  invalid/unavailable versions never fall back to the latest graph. Access is
  checked again when opening. These are authenticated preview URLs, not public shares.
- Workflow previews reuse the editor's graph projection, custom nodes, edges,
  and Node Inspector, but keep snapshot/selection state isolated from the live
  editor. Dragging, connecting, deleting, saving and execution are unavailable.
- In the Preview pane, double-click opens a bottom Inspector. In a standalone
  tab, clicking a node opens a right Inspector (bottom on narrow screens).
  “Open latest canvas” is a separate, explicitly labeled navigation action.

The browser extension keeps URL content inline. No new browser permissions, cookie handling, proxying, or
embedding-policy bypass is introduced. Sites that reject iframe embedding
still need the existing open-in-new-tab fallback.

## Compatibility and completion

`render_choices(title, options, description?, multiple=false)` renders inline
radio/checkbox rows and waits inside the same MCP invocation. Nothing is
preselected; confirm requires at least one choice, and cancel is always available.
Its result is `{status: "selected"|"cancelled"|"expired", selected_ids, message}`.
Alternatively, pass `choice_set_id` from registered browser download metadata
and omit `options`. The host supplies immutable filenames and byte counts;
downloads require single selection. Unknown, revoked or another turn's sets
are rejected. The saved user decision—not an Agent-supplied candidate ID—is
the selection proof for an ambiguous download. Transfer approval remains separate.
Refreshing restores the pending card and draft; stopping the tool disables it.
A selection does not authorize a file transfer. Existing waiting preview cards
retain their historical behavior, but new previews do not advertise that flag.

- New tool calls use `render_preview` and `render_choices`; historical tool
  names in saved messages do not create callable compatibility endpoints.
- Persisted artifact types (`file_preview`, `url_preview`) do not change, so
  old cards and history require no migration.
- Document/diagram prompts, draw.io publication hints and automatic completion
  publication use the unified file call. Completion evidence remains tied to
  the current file hash. URL publication never counts as file delivery, and
  legacy file evidence remains accepted for older turns.

Regression coverage includes both modes, invalid sources, confirmation gates,
legacy dispatch through MCP, hidden aliases, capability ceilings, stale file
evidence, main-app sidebar expansion and compact-client behavior.
