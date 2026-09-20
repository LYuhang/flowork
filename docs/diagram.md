# Diagram

`/diagram` creates and refines diagrams with the official draw.io MCP running
inside the Chat sandbox. The result is a native draw.io file rather than a
Flowork-specific diagram schema, so the same workflow can cover flowcharts,
UML, ER, BPMN, architecture and network diagrams, mind maps, timelines,
wireframes, engineering stencils, and free-form canvases.

## How it works

1. The Agent uses the official MCP tools to author draw.io XML, search the
   official shape libraries, manage pages, and apply draw.io routing.
2. Flowork adds a thin file boundary that validates and atomically saves the
   result at `/data/diagrams/<name>.drawio`.
3. The Agent uses the sandbox-local official draw.io Desktop CLI to export the
   current source to PNG, examines the actual pixels, and corrects material
   visual defects when necessary. A thin Flowork launcher provides a disposable
   headless display inside the sandbox; draw.io remains the renderer.
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
remain native and can be read or changed by the Agent with the official
`list_pages`, `get_page`, and `set_page` MCP tools.

Flowork performs bounded XML safety and structural checks before publication.
Those checks catch malformed XML, unsafe declarations, duplicate cell IDs, and
dangling connector terminals; visual quality is still judged from the rendered
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
