#!/usr/bin/env python3
"""Agent-driven Browser CLI smoke acceptance via an existing playwright-cli session.

Only the real Agent operates target controls. The tester sends one side-panel
message, then observes durable run state, fixture DOM and signed VFS bytes.
No Browser CLI is invoked directly; no synthetic relay or credentials are used.
This is a repeatable smoke journey, not the complete Browser acceptance matrix.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shlex
import struct
import subprocess
import sys
import time
from urllib.parse import urlparse
import uuid


BINARY = bytes(range(256)) * 256
TERMINAL = {"completed", "failed", "cancelled"}
HTML = """<!doctype html><meta charset="utf-8"><title>Flowork CLI acceptance fixture</title>
<h1>Browser CLI acceptance</h1>
<label>Name <input id="name" aria-label="Name"></label>
<label>Mode <select id="mode" aria-label="Mode"><option>Basic</option><option>Advanced</option></select></label>
<button id="save">Save form</button><output id="saved"></output>
<label>Command <input id="command" aria-label="Command"></label><output id="key"></output>
<button id="dialog">Confirm action</button><output id="confirmed"></output>
<button id="popup">Open details</button>
<label>Upload proof <input id="upload" aria-label="Upload proof" type="file"></label><output id="uploaded"></output>
<a id="download" href="/binary" download="fixture.bin">Download binary</a>
<script>
const q=id=>document.getElementById(id);
q('save').onclick=()=>q('saved').textContent=JSON.stringify({name:q('name').value,mode:q('mode').value});
q('command').onkeydown=e=>{if(e.key==='Enter')q('key').textContent=q('command').value};
q('dialog').onclick=()=>q('confirmed').textContent=confirm('Confirm fixture action?')?'accepted':'rejected';
q('popup').onclick=()=>window.open('/details'+location.search,'_blank');
q('upload').onchange=async()=>{const f=q('upload').files[0];const b=await f.arrayBuffer();
 q('uploaded').textContent=JSON.stringify({bytes:b.byteLength,sha256:[...new Uint8Array(await crypto.subtle.digest('SHA-256',b))].map(x=>x.toString(16).padStart(2,'0')).join('')})};
</script>"""


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        body = BINARY if path == "/binary" else (
            b"<!doctype html><title>Fixture details</title><h1>POPUP_OK</h1>"
            if path == "/details" else HTML.encode()
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream" if path == "/binary" else "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if path == "/binary":
            self.send_header("Content-Disposition", 'attachment; filename="fixture.bin"')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def parse_cli_result(output: str) -> object:
    if "### Result\n" not in output:
        raise RuntimeError("playwright-cli returned no result; inspect/resume the same run, do not resend")
    result, _ = json.JSONDecoder().raw_decode(output.split("### Result\n", 1)[1].lstrip())
    return result


def browser_command(command: str) -> str | None:
    """Recognize actual CLI argv, not an echo or prose mentioning the CLI."""
    try:
        args = shlex.split(command)
    except ValueError:
        return None
    if args and Path(args[0]).name in {"bash", "sh"}:
        for index, arg in enumerate(args[1:], 1):
            if arg.startswith("-") and "c" in arg and index + 1 < len(args):
                return browser_command(args[index + 1])
    if len(args) >= 3 and (Path(args[0]).name == "flowork-cli" or args[0] in {"$FLOWORK_CLI_BIN", "${FLOWORK_CLI_BIN}"}) and args[1] == "browser":
        return args[2] if "--help" not in args else None
    return None


def tool_evidence(events: list[dict], paths: set[str]) -> dict:
    commands: set[str] = set()
    artifacts: dict[str, dict] = {}
    failures: list[dict] = []
    missing_receipts: list[dict] = []
    for event in events:
        if event.get("type") != "tool_end" or event.get("name") != "shell":
            continue
        command = browser_command(str((event.get("invocation") or {}).get("input", {}).get("command", "")))
        if command is None:
            continue
        terminal = False
        for line in str(event.get("content") or "").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            if item.get("status") == "succeeded":
                terminal = True
                commands.add(command)
                for artifact in item.get("artifacts", []):
                    if isinstance(artifact, dict) and artifact.get("file") in paths:
                        artifacts[artifact["file"]] = artifact
            elif item.get("status") in {"failed", "unknown"}:
                terminal = True
                failures.append({"command": command, "status": item["status"], "error": item.get("error"), "hint": item.get("hint")})
        if not terminal:
            # A later success for the same command name must not hide an early
            # empty/native-lost receipt or a progress-only completion event.
            missing_receipts.append({"command": command, "reason": "No structured terminal receipt in shell completion."})
    return {"commands": sorted(commands), "artifacts": artifacts, "failures": failures,
            "missing_receipts": missing_receipts}


class Cli:
    def __init__(self, executable: str, session: str, cwd: str):
        self.executable, self.session, self.cwd = executable, session, cwd

    def call(self, code: str) -> object:
        result = subprocess.run(
            [self.executable, f"-s={self.session}", "run-code", code],
            cwd=self.cwd, capture_output=True, text=True, check=False,
        )
        if "### Result\n" not in result.stdout:
            diagnostic = result.stdout.split("### Error", 1)[-1].split("###", 1)[0].strip()
            raise RuntimeError("playwright-cli observation failed; do not resend: " + (diagnostic[:2000] or f"exit {result.returncode}"))
        return parse_cli_result(result.stdout)

    def frame(self, body: str, config: dict) -> object:
        return self.call("async page => { const cfg=" + json.dumps(config) + "; " + """
const candidates=page.context().pages().filter(p=>p.url().startsWith('chrome-extension://') && p.url().includes('/sidepanel.html'))
 .flatMap(p=>p.frames().filter(f=>f.url().startsWith(cfg.web_base+'/embed/chat')));
if(candidates.length!==1)throw Error('Expected one real extension side-panel document');
const frame=candidates[0];
""" + body + " }")


def checkpoint(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.replace(path)


def prompt_for(state: dict) -> str:
    return (
        "/browser Browser CLI acceptance. Use only flowork-cli browser for browser operations. "
        f"Run tab-list and select the already-authorized test tab at {state['source_tab_url']}; do not touch other sites. "
        f"Navigate that tab to {state['case_url']}. Observe fresh refs after state changes. "
        f"Fill Name with {state['case_id']}, choose Advanced, click Save form. "
        "Type CHECK in Command and press Enter. Click Confirm action, accept its JavaScript dialog. "
        "Open details and verify POPUP_OK; leave that test popup open for independent observation. "
        f"Create a sandbox UTF-8 file {state['upload']} containing exactly "
        f"{json.dumps(state['upload_text'])}, upload it using Upload proof, verify its result. "
        f"Use browser download to save Download binary to {state['download']}. "
        f"Use browser screenshot to save the original fixture's full page to {state['screenshot']}. "
        "Use the image-viewing tool to inspect it if available. Do not use eval/run-code, "
        "direct HTTP requests or DOM injection as substitutes for these UI actions. "
        "Never print credentials. Poll any yielded shell session until terminal; "
        "do not finish while browser work is pending. Report failures honestly. "
        f"End with {state['marker']}."
    )


def submit(cli: Cli, state: dict) -> dict:
    return cli.frame(r"""
const composer=frame.locator('[data-role=agent-composer-input]');
if(await composer.getAttribute('data-chat-id')!==cfg.chat_id)throw Error('Select the expected Browser Chat before starting');
if(await frame.locator('[data-action=agent-composer-stop]').count())throw Error('Chat already has active work');
const targets=page.context().pages().filter(p=>p.url().replace(/\/$/,'')===cfg.source_tab_url.replace(/\/$/,''));
if(targets.length!==1)throw Error('Open exactly one fixture tab during test setup');
// In the headless acceptance profile the real side-panel document is hosted
// in a separate tab. Keep the intended webpage active for the extension's
// initial control grant; focusing the extension tab would grant no web target.
// Dispatch only the actual Chat button's click (never a messages API call).
await composer.fill(cfg.prompt);
await targets[0].bringToFront();
const pending=frame.page().context().waitForEvent('request',{timeout:20000,predicate:r=>
 r.method()==='POST' && r.url().split('?')[0].endsWith('/chats/'+cfg.chat_id+'/messages')});
pending.catch(()=>{});
await frame.locator('[data-action=agent-composer-send]').dispatchEvent('click');
const request=await pending; const body=request.postDataJSON();
if(body?.client_request_id)return {client_request_id:body.client_request_id};
// Chrome may omit postData from debugger observations. The response header is
// another authoritative correlation; never repeat the POST to obtain its body.
const response=await request.response();const run_id=response && await response.headerValue('x-turn-id');
if(!run_id)throw Error('Submission observed without a durable request/run ID; do not resend');
return {run_id};
""", {**state, "prompt": prompt_for(state)})


def observe_run(cli: Cli, state: dict) -> dict:
    if state.get("status") in TERMINAL:
        return {"run_id": state["run_id"], "status": state["status"]}
    if not state.get("client_request_id"):
        return cli.frame(r"""
return frame.evaluate(async cfg=>{
 const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),4000);
 let text='';let status='running';let cursor=cfg.observed_event_id||0;
 try{const r=await fetch(cfg.web_base+'/api/v1/chats/'+cfg.chat_id+'/turns/'+cfg.run_id+'/stream',
  {signal:controller.signal,headers:{'Last-Event-ID':String(cursor)}});
  if(!r.ok)throw Error('Run stream observation HTTP '+r.status);
  const reader=r.body.getReader();const decoder=new TextDecoder();
  for(;;){const part=await reader.read();if(part.done)break;text+=decoder.decode(part.value,{stream:true});}}
 catch(error){if(error.name!=='AbortError')throw error;}finally{clearTimeout(timer);}
 const blocks=text.split(/\r?\n\r?\n/);blocks.pop(); // Never acknowledge a partial frame.
 for(const block of blocks){const lines=block.split(/\r?\n/);
  const id=Number(lines.find(l=>l.startsWith('id:'))?.slice(3));if(id>cursor)cursor=id;
  const kind=lines.find(l=>l.startsWith('event:'))?.slice(6).trim();
  if(kind==='done')status='completed';
  if(kind==='error'){const data=JSON.parse(lines.find(l=>l.startsWith('data:')).slice(5));status=data.code==='cancelled'?'cancelled':'failed';}}
 return {run_id:cfg.run_id,status,observed_event_id:cursor};},cfg);
""", state)
    return cli.frame("""
return frame.evaluate(async cfg=>{const r=await fetch(cfg.web_base+'/api/v1/chats/'+encodeURIComponent(cfg.chat_id)+
 '/turns/by-client-request/'+encodeURIComponent(cfg.client_request_id));
if(!r.ok)throw Error('Run observation HTTP '+r.status);const j=await r.json();return {run_id:j.run_id,status:j.status};},cfg);
""", state)


def read_events(cli: Cli, state: dict) -> list[dict]:
    return cli.frame(r"""
return frame.evaluate(async cfg=>{const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),15000);let text='';
try{const r=await fetch(cfg.web_base+'/api/v1/chats/'+cfg.chat_id+'/turns/'+cfg.run_id+'/stream',{signal:controller.signal});
if(!r.ok)throw Error('Event observation HTTP '+r.status);const reader=r.body.getReader();const decoder=new TextDecoder();
for(;;){const part=await reader.read();if(part.done)break;text+=decoder.decode(part.value,{stream:true});}}
catch(e){if(e.name==='AbortError')throw Error('Event replay incomplete; resume the same run without resubmitting');throw e;}finally{clearTimeout(timeout);}
return text.split(/\r?\n\r?\n/).flatMap(block=>{const line=block.split(/\r?\n/).find(l=>l.startsWith('data:'));
if(!line)return [];try{const event=JSON.parse(line.slice(5));return event.type==='tool_end'?[event]:[];}catch{return [];}});},cfg);
""", state)


def observe_fixture(cli: Cli, state: dict) -> dict:
    return cli.call("async page => { const cfg=" + json.dumps(state) + "; " + """
const target=page.context().pages().find(p=>p.url()===cfg.case_url);
if(!target)throw Error('Agent did not navigate to this exact case URL');
const observed=await target.evaluate(()=>({saved:document.querySelector('#saved').textContent,
 key:document.querySelector('#key').textContent,confirmed:document.querySelector('#confirmed').textContent,
 uploaded:document.querySelector('#uploaded').textContent}));
const popup=page.context().pages().find(p=>p.url()===cfg.fixture_url+'/details?case='+cfg.case_id);
observed.popup=!!popup && await popup.locator('h1').textContent()==='POPUP_OK';return observed; }""")


def read_artifact(cli: Cli, state: dict, path: str) -> bytes:
    result = cli.frame("""
return frame.evaluate(async cfg=>{const r=await fetch(cfg.web_base+'/api/v1/chats/workspace?chat_id='+encodeURIComponent(cfg.chat_id));
if(!r.ok)throw Error('Workspace observation HTTP '+r.status);const workspace=await r.json();
const cookies=new Map(document.cookie.split(';').map(v=>v.trim().split('=')));
const token=['__Host-vibecanvas-extension-csrf','vibecanvas-extension-csrf','__Host-vibecanvas-web-csrf','vibecanvas-web-csrf'].map(k=>cookies.get(k)).find(Boolean);
const signed=await fetch(cfg.web_base+'/api/v1/vfs/sign',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(token||'')},
 body:JSON.stringify({wf_id:workspace.workspace_scope_id,path:cfg.path})});
if(!signed.ok)throw Error('VFS signing HTTP '+signed.status);const {url}=await signed.json();const response=await fetch(url);
if(!response.ok)throw Error('VFS read HTTP '+response.status);const bytes=new Uint8Array(await response.arrayBuffer());
let binary='';for(const b of bytes)binary+=String.fromCharCode(b);return {base64:btoa(binary)};},cfg);
""", {**state, "path": path})
    return base64.b64decode(result["base64"], validate=True)


def validate_results(state: dict, observed: dict, evidence: dict, files: dict[str, bytes]) -> dict:
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)

    require(evidence.get("missing_receipts") == [], "CLI completion receipts are missing; inspect the same run")
    require(evidence.get("failures") == [], "CLI failures occurred; review Agent recovery before accepting this journey")
    saved = json.loads(observed["saved"])
    uploaded = json.loads(observed["uploaded"])
    require(saved == {"name": state["case_id"], "mode": "Advanced"}, "Saved form mismatch")
    require(observed["key"] == "CHECK" and observed["confirmed"] == "accepted" and observed["popup"], "Keyboard/dialog/popup result missing")
    upload = state["upload_text"].encode()
    require(uploaded == {"bytes": len(upload), "sha256": hashlib.sha256(upload).hexdigest()}, "Uploaded browser bytes differ")
    required = {"tab-list", "goto", "snapshot", "fill", "select", "click", "press", "dialog-accept", "upload", "download", "screenshot"}
    require(required <= set(evidence["commands"]), f"Successful CLI evidence missing: {sorted(required - set(evidence['commands']))}")
    require(files[state["download"]] == BINARY, "Downloaded VFS bytes differ")
    png = files[state["screenshot"]]
    require(len(png) >= 24 and png[:8] == b"\x89PNG\r\n\x1a\n" and png[12:16] == b"IHDR", "Screenshot is not a PNG")
    width, height = struct.unpack(">II", png[16:24])
    require(width > 0 and height > 0, "Empty screenshot")
    for path, content in files.items():
        receipt = evidence["artifacts"].get(path, {})
        require(receipt.get("persistence") == "durable", f"No durable receipt for {path}")
        require(receipt.get("bytes") == len(content) and receipt.get("sha256") == hashlib.sha256(content).hexdigest(), f"VFS receipt mismatch for {path}")
    return {"screenshot_width": width, "screenshot_height": height, "download_bytes": len(BINARY), "upload_bytes": len(upload)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve_fixture", type=int, metavar="PORT", help="Run a loopback-only fixture server separately; keep it alive until the Agent is terminal")
    parser.add_argument("--playwright_cli", default="playwright-cli")
    parser.add_argument("--session", default="browser-cli-acceptance", help="Existing authenticated playwright-cli session with the real unpacked extension")
    parser.add_argument("--cwd", default=str(Path.cwd()), help="Working directory used when the playwright-cli session was opened")
    parser.add_argument("--web_base", default="http://localhost:9001")
    parser.add_argument("--fixture_url", default="http://127.0.0.1:9120", help="Open this fixture manually during setup, then select the intended Browser Chat")
    parser.add_argument("--source_tab_url", help="Exact URL of an already-authorized disposable test tab; the Agent navigates it to the fixture. Defaults to fixture_url")
    parser.add_argument("--chat_id", help="Expected existing Chat ID; checked against the composer before sending")
    parser.add_argument("--evidence_dir", default="output/playwright/browser-cli-agent")
    parser.add_argument("--resume", type=Path, help="Observe an existing checkpoint; never submit another message")
    parser.add_argument("--max_wait_seconds", type=float, default=0, help="Optional observation budget only; exit 3 leaves browser and Agent running. Default: no deadline")
    args = parser.parse_args(argv)
    if args.serve_fixture:
        ThreadingHTTPServer(("127.0.0.1", args.serve_fixture), FixtureHandler).serve_forever()
        return 0
    if args.max_wait_seconds < 0:
        parser.error("--max_wait_seconds must not be negative")
    cli = Cli(args.playwright_cli, args.session, args.cwd)
    if args.resume:
        state_file = args.resume.resolve()
        state = json.loads(state_file.read_text())
        if not state.get("client_request_id") and not state.get("run_id"):
            parser.error("Submission outcome is unknown; reconcile the existing chat before resuming. Do not resend.")
    else:
        if not args.chat_id:
            parser.error("--chat_id is required for a new journey")
        case = uuid.uuid4().hex[:12]
        state = {"case_id": case, "chat_id": args.chat_id, "web_base": args.web_base.rstrip("/"), "fixture_url": args.fixture_url.rstrip("/"),
                 "source_tab_url": args.source_tab_url or args.fixture_url,
                 "case_url": args.fixture_url.rstrip("/") + "/?case=" + case, "marker": "CLI_ACCEPTANCE_" + case,
                 "upload": f"/data/acceptance-{case}.txt", "upload_text": f"Flowork CLI {case}\n",
                 "download": f"/data/acceptance-{case}.bin", "screenshot": f"/data/acceptance-{case}.png", "phase": "submitting"}
        directory = Path(args.evidence_dir).resolve() / case
        directory.mkdir(parents=True, exist_ok=False)
        state_file = directory / "run.json"
        checkpoint(state_file, state)  # Before clicking: an uncertain send is never retried.
        try:
            state.update(submit(cli, state), phase="observing")
            checkpoint(state_file, state)
        except Exception:
            print(f"Submission unconfirmed. Inspect the same Chat; do not resend. Checkpoint: {state_file}", file=sys.stderr)
            raise
    print(json.dumps({"checkpoint": str(state_file), "phase": state["phase"]}), flush=True)
    started = time.monotonic()
    while True:
        try:
            run = observe_run(cli, state)
            state.update(run)
            checkpoint(state_file, state)
            if run["status"] in TERMINAL:
                break
        except Exception as error:
            print(json.dumps({"observation_error": str(error), "action": "Observe the same run; no resubmission"}), flush=True)
        if args.max_wait_seconds and time.monotonic() - started >= args.max_wait_seconds:
            print(json.dumps({"status": "incomplete", "reason": "observation_budget", "checkpoint": str(state_file)}))
            return 3
        time.sleep(2)
    if state["status"] != "completed":
        print(json.dumps({"status": "failed", "run_id": state["run_id"], "agent_status": state["status"]}))
        return 1
    paths = {state["download"], state["screenshot"]}
    try:
        evidence = tool_evidence(read_events(cli, state), paths)
        state.update(evidence=evidence)
        observed = observe_fixture(cli, state)
        state.update(observed=observed)
        files = {path: read_artifact(cli, state, path) for path in paths}
        facts = validate_results(state, observed, evidence, files)
    except Exception as error:
        state.update(phase="verification_failed", verification_error=str(error))
        checkpoint(state_file, state)
        print(json.dumps({"status": "unverified", "error": str(error), "checkpoint": str(state_file),
                          "action": "Inspect the evidence or resume the same run; do not resend"}))
        return 1
    directory = state_file.parent
    (directory / "agent-screenshot.png").write_bytes(files[state["screenshot"]])
    state.update(phase="passed", evidence=evidence, observed=observed, facts=facts)
    checkpoint(state_file, state)
    print(json.dumps({"status": "passed", "scope": "CLI smoke journey only", "run_id": state["run_id"], "checkpoint": str(state_file), **facts}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
