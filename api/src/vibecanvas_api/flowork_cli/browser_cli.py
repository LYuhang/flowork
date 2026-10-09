"""Stdlib-only Browser CLI contract shared by client and trusted gateway.

Explicit targets do not replace live browser authorization. This module never
opens a browser, reads credentials or executes page code itself.
"""

from __future__ import annotations

import math
import os


def option(kind=str, *, required=False, default=None, choices=None, help="", repeat=False):
    return dict(kind=kind, required=required, default=default, choices=choices, help=help, repeat=repeat)


TAB = option(required=True, help="Platform tab_id from tab-list; not a list index, Chrome tab number or URL.")
REF = option(help="Fresh element reference from snapshot/find of this tab. Navigation can invalidate it.")
LOCATOR = option(help="CSS selector or supported Playwright locator, e.g. getByRole('button', {name:'Submit', exact:true}). Not arbitrary JavaScript. Must resolve to exactly one element.")
TIMEOUT = option(float, default=30.0, help="Timeout in seconds for each action/wait and its follow-up snapshot (default 30); 0 disables it. Not a whole CLI duration limit.")
OUTPUT = option(help="Sandbox output path; parent must exist. Existing files are not overwritten. Omit for the command's normal stdout/default artifact result.")
BUTTON = option(default="left", choices=("left", "right", "middle"))
TARGET = {"ref": REF, "locator": LOCATOR}
WORKER_LIFETIME = (
    "Browser work belongs to the current Agent turn, not a durable task. "
    "If the shell yields a background session, poll the same session until the "
    "command finishes or the user cancels. Ending the Agent turn cancels unfinished "
    "browser work; a shell session ID does not mean success."
)

# The inventory is intentionally public to tests: every command must have
# parser, runtime handler and real-extension evidence before MCP retirement.
COMMANDS = {
    "tab-list": ("List this Chat's live authorized tabs, including window_id, title, URL and active flag. No implicit current tab.", {}),
    "tab-info": ("Read the explicit tab's current URL, title, window and connection state.", {"tab_id": TAB}),
    "tab-new": ("Create a tab in the authorized opener's window; return its new platform tab_id. Does not authorize other existing tabs.", {"opener_tab_id": TAB, "url": option(default="about:blank"), "timeout": TIMEOUT}),
    "tab-close": ("Close only the specified authorized tab, never the whole browser.", {"tab_id": TAB}),
    "snapshot": ("Observe the page or an element. Returns readable structure with fresh refs; --boxes adds viewport CSS-pixel coordinates. Partial depth is not the whole document. With --output-file, the saved artifact is UTF-8 snapshot text, not JSON; use a .txt path and read it as text. The command receipt on stdout is JSON.", {"tab_id": TAB, **TARGET, "depth": option(int), "boxes": option(bool, default=False), "output_file": OUTPUT, "timeout": TIMEOUT}),
    "find": ("Search the current snapshot and return matching nodes/refs with context. --regex treats --text as a regular expression.", {"tab_id": TAB, "text": option(required=True), "regex": option(bool, default=False), "timeout": TIMEOUT}),
    "frame-list": ("List live frames and their frame_id, parent, URL and name. Frame IDs become stale after replacement/reconnection.", {"tab_id": TAB}),
    "screenshot": ("Save PNG in the sandbox and return its path, never base64. Default viewport; --full-page and an element target are exclusive. --hires uses device pixel ratio.", {"tab_id": TAB, **TARGET, "full_page": option(bool, default=False), "hires": option(bool, default=False), "output_file": OUTPUT, "timeout": TIMEOUT}),
    "goto": ("Navigate the explicit tab. Existing element refs may become invalid; inspect the returned observation before interacting.", {"tab_id": TAB, "url": option(required=True), "timeout": TIMEOUT}),
    "go-back": ("Navigate backward in this tab's history.", {"tab_id": TAB, "timeout": TIMEOUT}),
    "go-forward": ("Navigate forward in this tab's history.", {"tab_id": TAB, "timeout": TIMEOUT}),
    "reload": ("Reload this tab; old references may become invalid.", {"tab_id": TAB, "timeout": TIMEOUT}),
    "click": ("Click one actionable element. Does not silently choose the first match or force a covered element. A post-action observation failure does not undo the click.", {"tab_id": TAB, **TARGET, "button": BUTTON, "modifiers": option(repeat=True, choices=("Alt", "Control", "ControlOrMeta", "Meta", "Shift")), "timeout": TIMEOUT}),
    "dblclick": ("Double-click one actionable element.", {"tab_id": TAB, **TARGET, "button": BUTTON, "timeout": TIMEOUT}),
    "hover": ("Hover over one actionable element.", {"tab_id": TAB, **TARGET, "timeout": TIMEOUT}),
    "fill": ("Replace all text in an input, textarea or contenteditable. Empty --text clears it. --submit presses Enter after filling.", {"tab_id": TAB, **TARGET, "text": option(required=True), "submit": option(bool, default=False), "timeout": TIMEOUT}),
    "type": ("Focus the target and type characters at its current caret/selection using keyboard events. Does not clear the field or move to the end automatically. To append to a single-line input, first press --key End on that target. Prefer fill to replace ordinary form fields.", {"tab_id": TAB, **TARGET, "text": option(required=True), "timeout": TIMEOUT}),
    "press": ("Press and release a key/chord, e.g. Enter or ControlOrMeta+A. Optional target focuses it first; otherwise uses this tab's focused element.", {"tab_id": TAB, **TARGET, "key": option(required=True), "timeout": TIMEOUT}),
    "select": ("Select native HTML select options by exact case-sensitive value, not by display label. Repeat --value for a multi-select. Snapshot labels do not prove option values; do not guess or lowercase them. If uncertain, inspect the select's options with eval before choosing. option_not_found returns current value/label pairs for recovery. For custom dropdowns use click.", {"tab_id": TAB, **TARGET, "value": option(required=True, repeat=True, help="Exact HTML option value; case-sensitive and possibly different from the displayed label."), "timeout": TIMEOUT}),
    "check": ("Ensure a checkbox/radio is checked; already checked is success.", {"tab_id": TAB, **TARGET, "timeout": TIMEOUT}),
    "uncheck": ("Ensure a checkbox is unchecked; already unchecked is success.", {"tab_id": TAB, **TARGET, "timeout": TIMEOUT}),
    "drag": ("Drag between two fresh element references in this tab. For a canvas gesture use run-code or low-level mouse commands.", {"tab_id": TAB, "from_ref": option(required=True), "to_ref": option(required=True), "timeout": TIMEOUT}),
    "scroll": ("Scroll the page or target region. Distances are CSS pixels; verify content after scrolling.", {"tab_id": TAB, **TARGET, "direction": option(required=True, choices=("up", "down", "left", "right")), "pixels": option(int, default=600), "timeout": TIMEOUT}),
    "wait-for": ("Wait for one text/ref/locator target. --text is an exact visible-text match, not a substring or regular expression. For text inside a longer message use --locator \"getByText('Ready', {exact:false})\" or a regex locator. Visible/hidden waits are preferred to sleeps. Does not mean a business task has completed.", {"tab_id": TAB, **TARGET, "text": option(), "state": option(default="visible", choices=("visible", "hidden", "attached", "detached")), "timeout": TIMEOUT}),
    "generate-locator": ("Generate a reusable Playwright locator for a fresh ref. Inspect generated code before using it in run-code.", {"tab_id": TAB, "ref": option(required=True)}),
    "highlight": ("Highlight an element in the real page; --hide removes a target highlight, or all highlights when no target is supplied.", {"tab_id": TAB, **TARGET, "hide": option(bool, default=False)}),
    "mousemove": ("Move the pointer to viewport CSS-pixel coordinates, not desktop/device pixels.", {"tab_id": TAB, "x": option(float, required=True), "y": option(float, required=True), "timeout": TIMEOUT}),
    "mousedown": ("Hold a mouse button in this tab until mouseup. For multi-step gestures prefer one run-code script with finally cleanup.", {"tab_id": TAB, "button": BUTTON, "timeout": TIMEOUT}),
    "mouseup": ("Release a mouse button held in this tab.", {"tab_id": TAB, "button": BUTTON, "timeout": TIMEOUT}),
    "mousewheel": ("Dispatch a mouse wheel delta in CSS pixels at this tab's pointer position. Positive delta_y scrolls down.", {"tab_id": TAB, "delta_x": option(float, required=True), "delta_y": option(float, required=True), "timeout": TIMEOUT}),
    "keydown": ("Hold a key until keyup in this tab. No implicit current-tab state; use finally cleanup in long scripts.", {"tab_id": TAB, "key": option(required=True), "timeout": TIMEOUT}),
    "keyup": ("Release a key held in this tab.", {"tab_id": TAB, "key": option(required=True), "timeout": TIMEOUT}),
    "eval": ("Evaluate an expression or invoked function IN THE WEBPAGE; await Promises. --file reads JavaScript source from a sandbox file, not a browser-local file. Optional --ref passes that element to a function (el => ...). Optional --frame-id selects a frame; default main frame. Return JSON-compatible plain data, not DOM/JS handles. Nested undefined, BigInt and non-finite numbers are errors: use explicit null for missing fields, e.g. document.querySelector('#name')?.value ?? null. This is not sandbox Node.js.", {"tab_id": TAB, "expression": option(), "file": option(), "ref": REF, "frame_id": option(), "output_file": OUTPUT}),
    "run-code": ("Run async (page) => { ... } IN THE AGENT SANDBOX with the explicit authorized Playwright page. --code or --file, not both. Browser APIs cannot expand tab authority. Sandbox fs is available; document/window belong inside page.evaluate. Return JSON-compatible data; use null for missing fields, not nested undefined. setInputFiles('/data/input.bin') and fileChooser.setFiles(...) transfer sandbox bytes to Chrome; they never read that path on the user's PC. This also applies to pages from context.pages(), popup events and opener(). Register download/popup/filechooser listeners BEFORE the triggering action. For a popup download, wait on the popup itself, not the original page. Download example: const waiting = page.waitForEvent('download'); await page.getByRole('link', {name:'Download'}).click(); const download = await waiting; await download.saveAs('/data/result.csv'); return {file:'/data/result.csv'}; Download saveAs writes actual sandbox bytes, not a client Downloads path. Await it before returning. No fixed total execution deadline; cancellation cannot undo already performed effects.", {"tab_id": TAB, "code": option(), "file": option(), "output_file": OUTPUT}),
    "cookie-list": ("List cookie metadata applicable to this tab's current URL. Values are never printed. This does not grant permission to export credentials.", {"tab_id": TAB}),
    "cookie-export": ("Export URL-applicable cookies ONLY after separate site-scoped user consent in the extension side panel. --file is a filename such as cookies.json, NOT a workspace path. Use the returned private temporary path for authenticated requests during this Agent turn. Managed files are removed on revocation or turn end, never persisted, previewed or shared. Do not print secrets or copy them to /data. Missing consent fails with guidance, not an indefinite approval wait. JSON file shape is {url, cookies: [...]}, not a top-level array; cookies retain their attributes including sensitive values. Netscape uses seven tab-separated fields and #HttpOnly_ prefixes; it rejects partitioned cookies it cannot represent. Inspect only metadata/counts when checking the file. Browser-side authentication does not guarantee sandbox requests will authenticate.", {"tab_id": TAB, "file": option(required=True, help="Private output filename only, e.g. cookies.txt; the result returns its actual turn-private sandbox path."), "format": option(default="json", choices=("json", "netscape"))}),
    "upload": ("Transfer actual sandbox file bytes to a file input or pending page file chooser. Repeat --file for multiple files. Files are frozen before approval; no bytes or input/change events reach the page until approval mode permits transfer. Keep polling the same command while approval is pending. Does not read arbitrary paths on the user's computer. Optional ref/locator identifies the input directly.", {"tab_id": TAB, **TARGET, "file": option(required=True, repeat=True), "timeout": TIMEOUT}),
    "drop": ("Drop sandbox files OR MIME data on a target. Repeat --file or --data; never combine them. Files follow the same approval policy as upload, before bytes or drop events reach the page. Text-only MIME data needs no file-transfer approval. Data format: --data 'text/plain=hello'. This is not dragging an existing page element.", {"tab_id": TAB, **TARGET, "file": option(repeat=True), "data": option(repeat=True), "timeout": TIMEOUT}),
    "download": ("Observe BEFORE clicking one actual download control. This command does not fetch an arbitrary URL or convert navigation into a download. A cross-origin <a download> may open the resource instead; use the site's native download/export control or a response served as an attachment. Chrome saves the file on the user's computer; transfer to the sandbox follows approval mode. First use may require enabling Allow access to file URLs in extension details. Blob/data downloads without proven tab identity first wait for extension-local user confirmation, even in always_allow mode; no candidate metadata leaves the extension before confirmation. Keep polling the original command; never automate this confirmation. Local file confirmation does not grant transfer approval. A single candidate is transferred after approval; multiple candidates return status=selection_required, choice_set_id and metadata WITHOUT file bytes. Call render_choices with that choice_set_id, then download-receive with its confirmed candidate_id; on cancellation use download-cancel. Never repeat the click to resume a transfer. Requires --file and one ref/locator. Use /data for durable storage; success has a byte-count/hash/persistence receipt. Existing files are not overwritten. --timeout limits waiting for download start, not user approval or transfer duration. A Chrome Downloads path is not a saved sandbox file. If persistence fails, inspect the saved file rather than clicking again. Example: flowork-cli browser download --tab-id TAB --locator '#download' --file /data/result.csv", {"tab_id": TAB, **TARGET, "file": option(required=True), "timeout": TIMEOUT}),
    "download-status": ("Inspect an existing download choice_set_id in this tab and Agent turn. Returns registered metadata only, never file bytes. If the candidate set changed, use the newly returned choice_set_id and ask the user again.", {"tab_id": TAB, "choice_set_id": option(required=True)}),
    "download-receive": ("Transfer a registered local download WITHOUT clicking or downloading again. Requires choice_set_id and candidate_id from the same active turn; for multiple candidates the backend verifies the saved render_choices selection. File-transfer approval is separate, including after selection. --file is a new sandbox destination; /data is durable. The user's local file is kept. Return byte count/hash and storage receipt, not guessed success.", {"tab_id": TAB, "choice_set_id": option(required=True), "candidate_id": option(required=True), "file": option(required=True)}),
    "download-cancel": ("Release a pending download choice_set_id in this tab. Does not delete or cancel the user's local Chrome file and does not trigger another download.", {"tab_id": TAB, "choice_set_id": option(required=True)}),
    "dialog-accept": ("Accept the pending JavaScript alert/confirm/prompt in this tab. --text supplies a prompt response. Not an operating-system file picker.", {"tab_id": TAB, "text": option(), "timeout": TIMEOUT}),
    "dialog-dismiss": ("Dismiss the pending JavaScript dialog in this tab.", {"tab_id": TAB, "timeout": TIMEOUT}),
    "console": ("Read captured console messages; no guarantee of pre-connection history. --after uses the opaque cursor from a previous result. Sensitive values must not be exposed.", {"tab_id": TAB, "level": option(default="info", choices=("debug", "info", "warning", "error")), "after": option(), "limit": option(int, default=50)}),
    "requests": ("List captured network requests, not a command to send HTTP requests. Use returned request_id for details. No guarantee of pre-connection history.", {"tab_id": TAB, "url_contains": option(), "after": option(), "limit": option(int, default=50)}),
    "request": ("Read one captured request/response without replaying it. --request-id comes from requests. Unavailable response bodies are explicit errors, not empty successes. --output-file saves available response bytes in the sandbox.", {"tab_id": TAB, "request_id": option(required=True), "output_file": OUTPUT}),
}

READ_COMMANDS = frozenset({"tab-list", "tab-info", "snapshot", "find", "frame-list", "screenshot", "generate-locator", "cookie-list", "console", "requests", "request", "wait-for", "download-status"})
READ_OPERATIONS = frozenset("browser." + name for name in READ_COMMANDS)
OPERATIONS = frozenset("browser." + name for name in COMMANDS)
WRITE_OPERATIONS = OPERATIONS - READ_OPERATIONS
REQUIRED_TARGET = frozenset({"click", "dblclick", "hover", "fill", "type", "select", "check", "uncheck", "drop", "download"})


def add_parser(groups):
    group = groups.add_parser("browser", help="Operate explicitly authorized real browser tabs through the extension.",
        description="Agent-only, stateless target selection. No connect/disconnect/current tab. Start with tab-list, then snapshot, act, verify. Every page command names --tab-id. Files refer to this Chat's sandbox, not the user's computer. English JSON/JSONL stdout; exit 0 success, 1 failed/unknown, 2 invalid arguments. Never blindly replay an unknown write.", allow_abbrev=False)
    actions = group.add_subparsers(dest="action", required=True)
    for name, (description, options) in COMMANDS.items():
        detail = description + (" " + WORKER_LIFETIME if name in {"eval", "run-code", "download"} else "")
        command = actions.add_parser(name, help=description.split(". ")[0] + ".", description=detail, allow_abbrev=False)
        for key, spec in options.items():
            kwargs = {"required": spec["required"], "help": spec["help"] or key.replace("_", " ")}
            if spec["kind"] is bool:
                kwargs["action"] = "store_true"
            else:
                kwargs["type"] = spec["kind"]
                if spec["repeat"]:
                    kwargs["action"] = "append"
                if spec["choices"]:
                    kwargs["choices"] = spec["choices"]
            kwargs["default"] = spec["default"]
            command.add_argument("--" + key.replace("_", "-"), **kwargs)


def validate(operation, arguments):
    if operation not in OPERATIONS or not isinstance(arguments, dict):
        raise ValueError("Unsupported browser operation.")
    name = operation.removeprefix("browser.")
    options = COMMANDS[name][1]
    if set(arguments) - set(options):
        raise ValueError("Unsupported browser arguments: " + ", ".join(sorted(set(arguments) - set(options))))
    result = {}
    for key, spec in options.items():
        value = arguments.get(key, spec["default"])
        if value is None:
            if spec["required"]:
                raise ValueError(f"--{key.replace('_', '-')} is required.")
            continue
        values = value if spec["repeat"] else [value]
        if spec["repeat"] and (not isinstance(value, list) or not value):
            raise ValueError(f"--{key.replace('_', '-')} must be a non-empty list of values.")
        for item in values:
            kind = spec["kind"]
            valid = type(item) is kind or (kind is float and type(item) is int)
            if not valid or (type(item) is float and not math.isfinite(item)):
                raise ValueError(f"--{key.replace('_', '-')} has an invalid value.")
            if isinstance(item, str) and ("\x00" in item or (not item.strip() and key not in {"text", "value"})):
                raise ValueError(f"--{key.replace('_', '-')} must be non-empty and contain no NUL characters.")
            if spec["choices"] and item not in spec["choices"]:
                raise ValueError(f"--{key.replace('_', '-')} must be one of: {', '.join(spec['choices'])}.")
        result[key] = value
    targets = sum(key in result for key in TARGET)
    if targets > 1:
        raise ValueError("Use --ref or --locator, not both.")
    if name in REQUIRED_TARGET and targets != 1:
        raise ValueError("Exactly one of --ref or --locator is required.")
    if name == "wait-for" and targets + int("text" in result) != 1:
        raise ValueError("Exactly one of --text, --ref or --locator is required.")
    if name == "highlight" and not result.get("hide") and targets != 1:
        raise ValueError("Highlight requires --ref or --locator; use --hide alone to clear all highlights.")
    if name == "screenshot" and result.get("full_page") and targets:
        raise ValueError("--full-page cannot be combined with an element target.")
    if name in {"eval", "run-code"}:
        source = "expression" if name == "eval" else "code"
        if sum(key in result for key in (source, "file")) != 1:
            raise ValueError(f"Exactly one of --{source.replace('_', '-')} or --file is required.")
    if name == "drop":
        if sum(key in result for key in ("file", "data")) != 1:
            raise ValueError("Supply --file or --data, not both.")
        for item in result.get("data", []):
            mime, separator, _ = item.partition("=")
            if not separator or "/" not in mime or any(char.isspace() for char in mime):
                raise ValueError("--data must be MIME=value, e.g. text/plain=hello.")
    for key in ("timeout", "pixels", "depth", "limit"):
        if key in result and (result[key] < 0 or (key != "timeout" and result[key] == 0)):
            raise ValueError(f"--{key.replace('_', '-')} must be {'non-negative' if key == 'timeout' else 'positive'}.")
    if name in {"goto", "tab-new"}:
        url = result["url"]
        if not url.startswith(("http://", "https://")) and not (name == "tab-new" and url == "about:blank"):
            raise ValueError("--url must be HTTP(S); tab-new also accepts about:blank.")
    if name in {"find", "wait-for"} and "text" in result and not result["text"].strip():
        raise ValueError("--text must contain search text.")
    if name == "cookie-export" and (result["file"] in {".", ".."} or any(char in result["file"] for char in "/\\\r\n\t")):
        raise ValueError("Cookie --file must be a filename, not a path. Use the private path returned by the command.")
    return result


def execute(args, endpoint, core):
    operation = "browser." + args.action
    arguments = {key: getattr(args, key) for key in COMMANDS[args.action][1] if getattr(args, key) is not None}
    for key in ("file", "output_file"):
        if args.action == "cookie-export" and key == "file":
            continue
        if key in arguments:
            value = arguments[key]
            arguments[key] = [os.path.abspath(path) for path in value] if isinstance(value, list) else os.path.abspath(value)
    arguments = core.validate_arguments(operation, arguments)
    if not endpoint:
        result = core.error("runtime_unavailable", "No active Agent runtime is available.", "Run in an active Browser Chat with the extension connected.")
    else:
        try:
            result = core.request(endpoint, arguments, operation=operation)
        except (OSError, ValueError, KeyboardInterrupt):
            unknown = operation in WRITE_OPERATIONS
            result = {"status": "unknown" if unknown else "failed", "error": "result_unknown" if unknown else "runtime_unavailable",
                      "message": "The browser command was interrupted; its result is unavailable.",
                      "hint": "Inspect the tab and output files before retrying; the action may already have happened." if unknown else "Check the extension connection and retry this observation."}
    result.setdefault("status", "failed" if result.get("error") else "succeeded")
    return core.emit_result(result, exit_code=2 if result.get("error") == "invalid_arguments" else (0 if result.get("status") == "succeeded" else 1))
