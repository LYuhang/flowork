# Diagram

`/diagram` creates and refines native XML files using ordinary file tools and
`flowork-cli diagram` inside the Chat sandbox. The result is a native draw.io file rather than a
Flowork-specific diagram schema, so the same workflow can cover flowcharts,
UML, ER, BPMN, architecture and network diagrams, mind maps, timelines,
wireframes, engineering stencils, and free-form canvases.

## How it works

1. The Agent writes formatted, uncompressed draw.io XML, editing pages directly.
   `flowork-cli diagram search-shapes --query "aws s3"` returns official styles.
2. `flowork-cli diagram review --file /data/diagrams/<name>.drawio` checks the
   source without changing it. Compressed existing pages can also be reviewed.
3. `flowork-cli diagram render --file PATH` uses official draw.io Desktop to export the
   current source to PNG, examines the actual pixels, and corrects material
   visual defects when necessary. A thin Flowork launcher provides a disposable
   headless display inside the sandbox; draw.io remains the renderer. All pages
   render by default; optional `--pages "1-3,5"` selects 1-based page positions.
   By default stdout is one final JSON with status, source hash and PNG paths;
   page progress goes to stderr. Add `--stream` for stdout JSONL with
   `event=progress` followed by one `event=result|error`.
   There is no fixed total export deadline. Cancellation tears down the worker
   process group. Render never rewrites source or implicitly rearranges it.
4. The normal Sandbox-to-VFS lifecycle persists the accepted file and publishes it in
   the conversation.
5. Preview sends the exact file to the official diagrams.net renderer, then
   presents the returned SVG on a Flowork-native pan-and-zoom canvas. The Agent
   and user can inspect the rendered result and refine the source when needed.

The `.drawio` file is the only editable source of truth. Flowork does not keep
a second semantic model, diagram revision table, operation log, renderer, or
type-specific compiler.

## Preview

The conversation card provides a fitted overview with drag-to-pan, wheel zoom,
and Fit View. Open the full Preview for a larger read-only canvas. Revisions are
made by the Agent against the native file in its sandbox. Multi-page files
remain native and can be read or changed using ordinary XML tools. Diagram MCP
is retired; there is no connect/open/save state or alternate graph format.
Use `render_preview(type="file", source="/data/diagrams/name.drawio")` to publish.
Delivery requires successful review, rendering and image inspection of every
page of the current source revision. Editing source invalidates older evidence.

Flowork performs bounded XML safety and structural checks before publication.
Those checks catch malformed XML, unsafe declarations, duplicate page-local cell
IDs, dangling references, parent cycles and invalid geometry. Different pages
may reuse the same cell IDs. Visual quality is still judged from the rendered
diagram rather than inferred from XML validity. Agent feedback is generated
inside the sandbox and does not require the user to open Preview first.

## Export

Preview downloads the exact native source directly. SVG and editable PNG are
rendered through the official diagrams.net embed protocol. PDF and JPG are
encoded locally from that official PNG render, which avoids requiring a
separate draw.io export server while preserving the same visual result.

| Format | Best suited for |
| --- | --- |
| `.drawio` | Lossless editing in diagrams.net or draw.io Desktop |
| SVG | Scalable documentation and design-tool handoff |
| PNG | Chat, presentations, and general image use |
| PDF | Documents, printing, and formal delivery |
| JPG | Compact bitmap delivery when transparency is unnecessary |

SVG and PNG exports include the draw.io source where supported by the official
format, so they can be reopened in draw.io. `.drawio` remains the canonical
file used for future Agent changes.
