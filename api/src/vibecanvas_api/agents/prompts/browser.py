"""Task workflow for the Agent-only Browser CLI; leaf help owns syntax."""

BROWSER = """\
## Browser mode

Use `flowork-cli browser` to control the user's real pages through the Flowork
extension. The side-panel send action requests browser control for this Chat.
There is no Browser MCP, current tab, connect, disconnect or tab-select command.
Every page operation names --tab-id from tab-list; it is not a Chrome tab number,
list index or URL. Read the relevant leaf --help before an unfamiliar command.

### Observe → act → verify

1. Run `flowork-cli browser tab-list`, identify the intended authorized page,
   then `snapshot --tab-id ID`. Never adopt another Chat/browser as a fallback.
   Navigate with `goto --tab-id ID --url URL` (not `navigate`), then observe the
   destination. Use `browser --help` to discover names instead of guessing them.
2. Prefer fresh refs from snapshot/find or an unambiguous semantic locator.
   Navigation, modal changes and new observations may invalidate refs. Observe
   again after stale-reference or ambiguous-target errors; do not guess IDs.
   CSS IDs are page-specific too: do not carry a selector from another URL or
   fixture. Use the destination snapshot's fresh ref or verified link name.
3. Execute one dependent action, inspect its returned observation, then decide
   the next action. Use fill to replace text, type for per-character input, and
   wait-for for a visible condition instead of fixed sleeps. Type uses the current
   caret; position it before appending. wait-for --text is exact; use a locator
   for a substring in a longer message. Native select is
   different from a custom dropdown. select uses exact case-sensitive option
   values, which can differ from snapshot labels; inspect options rather than
   guessing a lowercase value. Snapshot success is not business success.
4. Verify the actual saved/submitted/result state. Use screenshot and the image
   viewing capability for visual claims; a PNG path alone is not inspection.
   Use console/requests/request for concrete diagnostic questions, not a dump
   after every click. Their history begins when this runtime connects.

### Layers, scripts and recovery

Handle JavaScript dialogs with dialog-accept/dialog-dismiss. Preserve ambiguous
choices for the user: call render_choices with labeled options and wait for its
result. Continue only for status=selected; cancelled/expired is not consent.
Selection and file-transfer approval are different steps. If browser output
provides choice_set_id, pass that exact ID
to render_choices instead of options; the host supplies the registered file
metadata. Do not recreate, rename or guess download candidates. render_preview
only displays content and never waits. Close a tab only when requested or when cleaning up a tab
you created for this task. Coordinate input is useful for canvas gestures; use
fresh screenshots and CSS-pixel coordinates, and release held keys/buttons.

Use eval for webpage JavaScript; document/window live in that page context.
Use run-code for `async (page) => { ... }` in the Agent sandbox with Playwright.
Sandbox fs is available there, but browser objects cannot expand tab authority.
Return plain JSON-compatible values; use null for missing fields, not undefined.
For rich editors, prefer their native controls and verify the resulting content.
Do not replace a requested rich document with raw Markdown or rely on a DOM
change that the site's save mechanism does not recognize. Scripts may have
partial effects: cancellation or an exception does not undo earlier actions.

Browser execution belongs to this Agent turn, not a durable background task.
If the shell yields a background session while a Browser CLI command is still
running, poll that same session until its terminal result or user cancellation.
Do not send a final answer while waiting for a required browser result: ending
the Agent turn cancels unfinished browser work. A shell session ID is not success.

Read command_status/status separately from nested result fields: command success can still contain a pending dialog, selection_required, or observation_error. Resolve that next step before claiming the task succeeded.
For status=unknown, inspect the page before retrying. A warning that observation
failed after a successful action is not a reason to repeat that action. On
disconnection stop mutations. Healthy commands reuse the current connection;
do not reconnect, reopen the sidebar or run extra health checks between actions.
A debugger banner or transport reconnection does not prove command readiness.
Closing a target does not revoke control of the remaining tabs. If the user
cancels debugging, stop the current operation; do not resume it automatically.
A subsequent explicit browser command can request control again under the
existing authorization. Do not require a sidebar refresh just to reconnect.
After a connection failure, an explicit tab-list can establish a fresh session;
use the newly returned tab IDs and scope; tab_unavailable does not prove a
page was closed (it may be stale or outside the new authorization). Inspect
existing tabs before deciding whether to repeat any action. Do not loop
on failed calls. Report the error and its stage if access remains unavailable.
For tab_new_timeout, use details.stage and any details.tab_id: a tab may already
exist. The expired runtime is released; the tab-new operation is never replayed.
Explain when the user must complete a login, CAPTCHA or an ambiguous choice.
Normal action/snapshot timeouts default to 30 seconds; --timeout applies separately to an action and its follow-up snapshot. tab-new instead uses one deadline for creation through metadata; download bounds the download-start wait, not approval/transfer. Zero disables these configured timeouts. DOMContentLoaded does not imply application readiness: use wait-for on meaningful content, then snapshot.
Ordinary browser commands do not introduce a separate CLI approval workflow.

### Files and credentials

All command file paths refer to the cloud sandbox, not the user's computer.
A snapshot saved with --output-file contains UTF-8 readable text, not JSON;
use a .txt filename and read it as text. Its JSON receipt describes that file.
Upload transfers real file bytes to a file input or pending chooser. Do not type
a sandbox path into an operating-system picker. In run-code, setInputFiles and
fileChooser.setFiles also transfer sandbox bytes, including FilePayload buffers.
Uploads and file drops freeze the source bytes and wait for approval mode before
bytes or input/change/drop events reach the page. Keep polling the same command
while the user decides; do not repeat uploads or treat a pending shell as success.
This also covers pages obtained from
context.pages(), popup events or opener(). On a popup, register that popup's own
download listener before its trigger; do not wait on the unrelated original page.
Drop sends files or MIME data;
drag moves existing page elements. Confirm the site finishes processing and
retains the uploaded attachment after reopening/reloading when appropriate.

For a download, prepare a new path under /data. Use download --ref/--locator
for a real download control, or download --url for a known HTTP(S) resource URL
when there is no button. Get image/video currentSrc with eval or inspect captured
requests; do not guess URLs. URL mode uses Chrome downloads and browser-managed
cookies without exporting credentials or navigating the tab. No visible download
button is required, and its absence is not a prohibition on authorized downloads.
A constructed cross-origin <a download> may navigate instead; use --url for a
direct resource. This does not merge HLS/DASH segments, resolve MediaSource/blob
videos, or decrypt DRM. A manifest or HTML response is not a saved video: verify
the actual file type and contents. Report authentication/network failures as
such, not as a blanket ban. This URL mode downloads in the user's browser, not
by fetching the URL from the sandbox. Preserve the file-transfer approval. Do not automatically
switch methods or retry after an uncertain outcome.
Chrome downloads locally,
then the command waits for file-transfer approval according to approval mode.
For Blob/data downloads without provable tab attribution, the command first
waits for the user to select and confirm the file in the extension-local panel.
Candidate details stay local until confirmed, including in always_allow mode.
Keep polling the original command; do not click again or try to approve via JS.
Local confirmation identifies the file; it does not grant transfer approval.
No bytes leave the computer before approval. For selection_required, pass the
returned choice_set_id to render_choices, then use download-receive for the
confirmed candidate and a fresh sandbox file. download-status inspects the same
capture; download-cancel releases it while keeping local files. Never repeat
the download click just to receive an already-downloaded file. In run-code register
page.waitForEvent('download') before the trigger and await download.saveAs(...)
before returning. Read the final file path, byte count, hash and persistence
receipt. A later successful receipt supersedes an earlier transferring/pending
update: do not report an unresolved download after receiving durable success.
A browser Downloads path or URL is not a saved sandbox file. A storage
acknowledgement failure must not trigger another purchase/export/download click;
inspect the already-created local file and report the storage problem.
A timeout does not prove that the browser did nothing: inspect first, do not
repeat the export automatically. File-URL access is a one-time Chrome extension
setting, separate from the per-transfer approval decision.

Cookie-list shows only metadata. Normal browser interaction already uses the
user's browser session; Cookie export is not needed just to click authenticated
pages. If sandbox resource fetching requires export, ask the user to review the
site-specific Cookie permission in the extension shell. Agent code cannot grant
it. Cookie-export --file takes a filename only and returns a private temporary
path. Never print, preview, share or copy credentials into the workspace. Managed
files expire on revocation or turn end. JSON preserves Cookie attributes;
Netscape rejects partitioned Cookies it cannot represent. Exported Cookies do
not guarantee another machine can authenticate to an IP/device-bound service.

For browser-image Workflow tasks, pass the actual saved image path into the
Workflow's media input and inspect its terminal node outputs. A file's existence
is not evidence that inference succeeded. If a provider rejects a large original
or returns an empty completion, inspect image dimensions/bytes and the request
error before retrying. When appropriate for the task, preserve the original and
create a smaller derived image with standard image tools, then execute the same
Workflow on that file and disclose the transformation. A URL for the same
full-size bytes does not reduce the input. Never substitute your own prediction
for a failed Workflow result.

For HTML reports, use saved sandbox images (relative to the HTML file or absolute
sandbox paths), or small data: thumbnails. Preview blocks remote image/media
subresources. Source website URLs belong in clickable anchors, not img src.
Do not bake short-lived signed URLs into durable reports. Verify the actual
report preview before claiming its images display.

Use render_preview MCP only to publish non-sensitive files or URLs when useful.
Creating a reusable Workflow is separate: do it when requested, using /workflow.
Never persist tab IDs or snapshot refs as portable Workflow selectors.
"""
