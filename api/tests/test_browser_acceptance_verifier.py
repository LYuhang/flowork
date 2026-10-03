"""The acceptance observer must fail closed and never retry an Agent submission."""

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("browser_verifier", ROOT / "scripts/verify_real_sidepanel_browser_agent.py")
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class BrowserAcceptanceTests(unittest.TestCase):
    def test_cli_envelope(self):
        self.assertEqual(verifier.parse_cli_result('### Result\n{"ok":true}\n### Ran Playwright code\n'), {"ok": True})
        for invalid in ("### Error\nfailed", "### Result\n{unfinished"):
            with self.assertRaises((RuntimeError, ValueError)):
                verifier.parse_cli_result(invalid)

    def test_actual_commands_not_mentions(self):
        for command in ("flowork-cli browser snapshot --tab-id tab_1", "/bin/bash -lc 'flowork-cli browser snapshot --tab-id tab_1'", "$FLOWORK_CLI_BIN browser snapshot"):
            self.assertEqual(verifier.browser_command(command), "snapshot")
        for command in ("echo flowork-cli browser snapshot", "flowork-cli browser snapshot --help", "flowork-cli workflow list", "'bad"):
            self.assertIsNone(verifier.browser_command(command))

    def test_only_successful_shell_results_are_evidence(self):
        event = {"type": "tool_end", "name": "shell", "invocation": {"input": {"command": "flowork-cli browser screenshot"}},
                 "content": json.dumps({"status": "succeeded", "artifacts": [{"file": "/data/s.png", "persistence": "durable"}]})}
        result = verifier.tool_evidence([event], {"/data/s.png"})
        self.assertEqual(result["commands"], ["screenshot"])
        self.assertIn("/data/s.png", result["artifacts"])
        for overrides in ({"type": "message"}, {"name": "other"}, {"content": "Screenshot succeeded."},
                          {"content": '{"status":"failed","error":"file_not_found"}'}):
            self.assertEqual(verifier.tool_evidence([{**event, **overrides}], {"/data/s.png"})["commands"], [])

    def facts(self):
        state = {"case_id": "test", "upload_text": "proof\n", "download": "/data/test.bin", "screenshot": "/data/test.png"}
        files = {state["download"]: verifier.BINARY, state["screenshot"]: b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 100, 200)}
        observed = {"saved": '{"name":"test","mode":"Advanced"}', "key": "CHECK", "confirmed": "accepted", "popup": True,
                    "uploaded": json.dumps({"bytes": 6, "sha256": hashlib.sha256(b"proof\n").hexdigest()})}
        evidence = {"commands": ["tab-list", "goto", "snapshot", "fill", "select", "click", "press", "dialog-accept", "upload", "download", "screenshot"],
                    "artifacts": {path: {"persistence": "durable", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for path, data in files.items()},
                    "failures": [], "missing_receipts": []}
        return state, observed, evidence, files

    def test_later_same_command_success_cannot_hide_missing_receipt(self):
        event = {"type": "tool_end", "name": "shell",
                 "invocation": {"input": {"command": "flowork-cli browser fill"}}}
        for content in ("", "truncated output", '{"status":"running","message":"Waiting"}'):
            evidence = verifier.tool_evidence([
                {**event, "content": content},
                {**event, "content": '{"status":"succeeded"}'},
            ], set())
            self.assertEqual(evidence["commands"], ["fill"])
            self.assertEqual(len(evidence["missing_receipts"]), 1)
            self.assertEqual(evidence["missing_receipts"][0]["command"], "fill")

    def test_failed_or_unknown_results_are_terminal_but_not_passes(self):
        for status in ("failed", "unknown"):
            event = {"type": "tool_end", "name": "shell",
                     "invocation": {"input": {"command": "flowork-cli browser download"}},
                     "content": json.dumps({"status": status, "error": "fixture_error"})}
            evidence = verifier.tool_evidence([event], set())
            self.assertEqual(evidence["missing_receipts"], [])
            self.assertEqual(evidence["failures"][0]["status"], status)

    def test_correct_dom_and_files_cannot_mask_failed_or_missing_receipts(self):
        for key in ("failures", "missing_receipts"):
            state, observed, evidence, files = self.facts()
            evidence[key] = [{"command": "fill"}]
            with self.assertRaises(ValueError):
                verifier.validate_results(state, observed, evidence, files)

    def test_independent_bytes_dom_and_durable_receipts_required(self):
        good = self.facts()
        self.assertEqual(verifier.validate_results(*good)["download_bytes"], 65536)
        for change in ("dom", "upload", "commands", "binary", "png", "durability", "hash"):
            state, observed, evidence, files = copy.deepcopy(good)
            if change == "dom":
                observed["key"] = "wrong"
            elif change == "upload":
                observed["uploaded"] = '{}'
            elif change == "commands":
                evidence["commands"].remove("download")
            elif change == "binary":
                files[state["download"]] = b"partial"
            elif change == "png":
                files[state["screenshot"]] = b"not a screenshot"
            elif change == "durability":
                evidence["artifacts"][state["download"]]["persistence"] = "sandbox"
            else:
                evidence["artifacts"][state["download"]]["sha256"] = "wrong"
            with self.subTest(change=change), self.assertRaises(ValueError):
                verifier.validate_results(state, observed, evidence, files)

    def test_resume_budget_does_not_resubmit_or_close_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "run.json"
            verifier.checkpoint(checkpoint, {"client_request_id": "same-request", "phase": "observing"})
            with patch.object(verifier, "submit") as submit, patch.object(verifier.Cli, "call") as call, \
                    patch.object(verifier, "observe_run", return_value={"run_id": "same-run", "status": "running"}), \
                    patch.object(verifier.time, "monotonic", side_effect=[0, 10]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(verifier.main(["--resume", str(checkpoint), "--max_wait_seconds", "1"]), 3)
                submit.assert_not_called()
                call.assert_not_called()
            self.assertEqual(json.loads(checkpoint.read_text())["run_id"], "same-run")

    def test_submit_handles_chrome_omitting_post_data_without_vm_url_global(self):
        state = {"web_base": "http://localhost:9001", "fixture_url": "http://127.0.0.1:9120",
                 "source_tab_url": "http://127.0.0.1:9120", "chat_id": "chat", "case_url": "http://127.0.0.1:9120/?case=test",
                 "case_id": "test", "upload": "/data/t.txt", "upload_text": "test", "download": "/data/t.bin", "screenshot": "/data/t.png", "marker": "test"}
        cli = verifier.Cli("unused", "unused", ".")
        with patch.object(cli, "call", side_effect=lambda code: code):
            code = verifier.submit(cli, state)
        harness = r"""
const vm=require('node:vm'); let clicks=0; const focus=[];
const request={method:()=> 'POST',url:()=> 'http://localhost:9001/api/v1/chat-scopes/s/chats/chat/messages',
 postDataJSON:()=>null,response:async()=>({headerValue:async()=> 'run-from-header'})};
const input={getAttribute:async()=> 'chat',fill:async()=>{}};
const frame={url:()=> 'http://localhost:9001/embed/chat?mode=browser',page:()=>panel,
 locator:selector=>selector.includes('agent-composer-input')?input:
 selector.includes('agent-composer-stop')?{count:async()=>0}:{dispatchEvent:async type=>{
  if(type!=='click')throw Error('Wrong UI event');clicks++;}}};
const panel={url:()=> 'chrome-extension://real/sidepanel.html',frames:()=>[frame],context:()=>context,
 bringToFront:async()=>{focus.push('panel');}};
const target={url:()=> 'http://127.0.0.1:9120/',bringToFront:async()=>{focus.push('target');}};
const context={pages:()=>[panel,target],waitForEvent:async(name,options)=>{
 if(name!=='request'||!options.predicate(request))throw Error('Wrong request predicate');return request;}};
const page={context:()=>context};
const run=vm.runInNewContext('('+CODE+')',{});
run(page).then(result=>console.log(JSON.stringify({result,clicks,focus}))).catch(error=>{console.error(error);process.exitCode=1;});
""".replace("CODE", json.dumps(code))
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(json.loads(result.stdout), {
            "result": {"run_id": "run-from-header"}, "clicks": 1,
            "focus": ["target"],
        })

    def test_terminal_run_remains_terminal_when_resuming_verification(self):
        cli = verifier.Cli("unused", "unused", ".")
        with patch.object(cli, "call") as call:
            self.assertEqual(verifier.observe_run(cli, {"run_id": "same-run", "status": "completed", "observed_event_id": 42}),
                             {"run_id": "same-run", "status": "completed"})
            call.assert_not_called()

    def test_uncertain_submission_never_retries(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(verifier, "submit", side_effect=RuntimeError("observer lost")) as submit, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                verifier.main(["--chat_id", "chat", "--evidence_dir", directory])
            submit.assert_called_once()
            checkpoint = next(Path(directory).glob("*/run.json"))
            with self.assertRaises(SystemExit):
                verifier.main(["--resume", str(checkpoint)])
            submit.assert_called_once()

    def test_retired_mcp_entrypoint_cannot_claim_acceptance(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/verify_playwright_mcp_real_browser.py")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("RETIRED", result.stderr)


if __name__ == "__main__":
    unittest.main()
