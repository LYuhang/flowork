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

## File preview acceptance (2026-10-02)

This is a historical Chromium acceptance record, not a guarantee for every
browser/codec. The synchronous quote-selection effect mentioned at the end was
subsequently removed during the 2026-10-06 lint cleanup.

On the deployed native service, the Flowork Agent generated actual files in one
conversation and published a `render_preview` card for each file. Acceptance
clicked those cards and their standalone Preview links in Chromium, inspected
rendered content/screenshots, and checked the interactions below. Test account
credentials, signed resource URLs and private conversation history are not
stored in this repository.

| Samples | Verified behavior |
| --- | --- |
| DOCX, PPTX, PDF | Two pages; Chinese/English text; page navigation changes rendered content |
| XLSX | Formatting, cell values and both worksheets |
| Markdown | Table, code block, formula and relative image |
| HTML, HTM | Actual content inside the sandboxed preview iframe |
| TXT, JSON, Python, YAML, XML | Text/code content |
| CSV, TSV, JSONL, NDJSON | Parsed table headers and records |
| Draw.io | Native diagram with two labeled nodes and a connector |
| PNG, JPG, JPEG, GIF, WebP, SVG | Decoded images |
| WAV, MP3, MP4, WebM | Loaded metadata and advancing playback; video dimensions |

All 27 sample formats completed acceptance after these corrections:

- PDF.js previously loaded pages but omitted text from the non-embedded CJK
  font sample. The build now publishes the matching package's CMaps, fonts,
  profiles and decoder resources locally. Verification included visible text
  pixels, screenshot inspection and successful deployed resource requests.
- Markdown file previews now reuse the safe math renderer already used by
  chat messages, including TeX delimiter normalization and lazy-loaded styles.
  Regression coverage preserves literal code and disallows trusted TeX links.
- The native environment lacked FFmpeg for generating compressed media samples.
  Native bootstrap and the API image now install FFmpeg/FFprobe. The same Agent
  conversation then generated genuine MP3/H.264 MP4/WebM files for playback checks.

Draw.io uses the installed renderer served at `/api/v1/preview/drawio-assets/`;
ordinary previews do not send documents to diagrams.net. Wait for the diagram
to appear rather than accepting a loading state. This
matrix covers representative supported formats, not every possible encoding,
codec, malformed file or legacy Office extension. Legacy DOC/XLS/PPT and archive
preview support is unchanged.

Validation: seven Markdown preview regression tests, TypeScript build,
production Vite build, deployment-path guard and native bootstrap shell syntax
passed. Targeted ESLint still reports the pre-existing synchronous
`setQuoteSelection(null)` effect; running ESLint against the original HEAD file
reproduced the same finding. The new asset plugin and PDF renderer passed lint.

### Draw.io cold-cache loading

The installed renderer includes large JavaScript bundles. Compress its public
static assets at the reverse proxy; compressing only Vite's `/assets/` does not
cover this route. Docker's `web/nginx.conf` includes this configuration. Native
TLS proxies that forward `/api/` directly to the API need an equivalent location:

```nginx
location ^~ /api/v1/preview/drawio-assets/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $http_host;
    gzip on;
    gzip_vary on;
    gzip_types text/javascript application/javascript text/css application/json image/svg+xml;
}
```

Preserve the API's renderer-specific CSP and SAMEORIGIN headers; do not apply
the main application's frame prohibition. Keep compression scoped to these
public assets, outside authenticated responses and SSE. The preview permits
60 seconds for renderer initialization, then 30 seconds for export; both phases
are bounded and clean up their iframe when they finish or fail. No automatic
retry or external renderer fallback is performed.

## Example-driven live acceptance (2026-10-20)

On `flowork.top`, each of the 18 visible example cards was selected in a real
Chromium session and submitted to GPT-5.6-Terra in a dedicated QA project.
Tables received a synthetic workbook; the weekly report received synthetic
notes. Knowledge examples received an actual source attachment, and skill
updates used a new QA-only package. No existing user skill was updated.

| Examples | Preview checks |
| --- | --- |
| Sequence, architecture, process diagrams | Three generated Draw.io files; decoded diagram image, not just a successful resource request |
| Decision brief, presentation | Generated DOCX and PPTX, all three pages of each |
| Sales workbook | Data and Summary worksheets, including rendered charts |
| Table cleanup, amount audit | Five worksheets in each generated workbook, individually selected |
| Weekly report | Generated one-page DOCX |
| Knowledge create, explore, update | Downloaded README/source files; updated package version 2 and its README |
| Skill template, download, update | Actual SKILL.md files; QA-only update published as version 2 |
| Workflow automation | Saved-version preview with 11 rendered nodes and its Markdown evidence file |
| Scheduled task, API deployment | Actual detail pages; deployment's local JSON file in the code viewer |

Conversations were reopened before final card/link checks. The two document
link forms emitted by the Agent, `sandbox:/…` and `https://file+workspace/…`,
now resolve through the same authorized file preview path. Public views and
unsafe URL/path rejection retain their existing restrictions. Prompts request
actual absolute file links; skill directory paths are shown as code because a
directory is not a file preview target. An older directory link in the download
conversation remains unsupported; its actual SKILL.md link was verified.

The originally reported Draw.io file also passed a cold-cache desktop check
with approximately 2 Mbps download bandwidth and 100 ms latency: the diagram
appeared in about 60 seconds. A separate 390×844 Chromium viewport check passed
in about 18 seconds. This is not a physical iOS/Safari test. Large renderer
bundles still make first-load latency noticeable; the change fixes compression
and separates initialization/export deadlines, rather than promising instant
loading or unbounded waits.

The workflow example was explicitly restricted to building, validating and
previewing for this acceptance. Its full execution/approval and ten-minute
load-test instructions were not rerun here. The QA schedule was paused in its
page, and the QA deployment was disabled in Settings; the API subsequently
reported `stopped` with no runtime instances. Generated files and conversations
were retained. Credentials and private screenshots are not committed.
