#!/usr/bin/env python3
"""Real Custom Skill versioning and sandboxed Runtime acceptance test.

The verifier drives the same browser UI a user sees for Skill management, then
uses the public Chat/SSE contract used by that UI to prove that the installed
Agent Runtime receives the newly published Skill revision.

Expected environment:

* API at http://127.0.0.1:8000 with ENABLE_TEST_USER=1;
* web app at http://127.0.0.1:9001;
* Codex CLI authentication for the test user.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


class Api:
    def __init__(self, base_url: str) -> None:
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=300)
        self.token = ""

    def close(self) -> None:
        self.client.close()

    def login(self) -> None:
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": "test", "password": "test"},
        )
        response.raise_for_status()
        self.token = response.json()["session_token"]
        self.client.headers["Authorization"] = f"Bearer {self.token}"

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        response = self.client.get(path, **kwargs)
        response.raise_for_status()
        return response

    def post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        response = self.client.post(path, json=body)
        response.raise_for_status()
        return response

    def put(self, path: str, body: dict[str, Any]) -> httpx.Response:
        response = self.client.put(path, json=body)
        response.raise_for_status()
        return response

    def delete(self, path: str) -> None:
        response = self.client.delete(path)
        if response.status_code not in {204, 404}:
            response.raise_for_status()

    def stream_turn(
        self,
        *,
        scope_id: str,
        chat_id: str,
        content: str,
        model_id: str,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        settings: dict[str, Any] = {"model_id": model_id}
        if reasoning_effort:
            settings["reasoning_effort"] = reasoning_effort
        body = {
            "role": "user",
            "content": content,
            "client_request_id": f"skill-e2e-{uuid.uuid4().hex}",
            "mode": "chat",
            "approval_mode": "always_allow",
            "surface": "main",
            "agent_surface": "chat",
            "mcp_server_ids": [],
            "chat_config_revision": 0,
            "agent_settings": settings,
        }
        events: list[tuple[str, Any]] = []
        started_at = time.monotonic()
        path = f"/api/v1/chat-scopes/{scope_id}/chats/{chat_id}/messages"
        with self.client.stream("POST", path, json=body, timeout=300) as response:
            response.raise_for_status()
            turn_id = response.headers.get("x-turn-id")
            event_name = ""
            for line in response.iter_lines():
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    raw = line[5:].strip()
                    try:
                        data: Any = json.loads(raw)
                    except json.JSONDecodeError:
                        data = raw
                    events.append((event_name, data))

        visible_updates = [
            str(data.get("delta") or data.get("content") or "")
            for event_name, data in events
            if event_name == "CHAT_EVENT"
            and isinstance(data, dict)
            and data.get("type") in {"message_delta", "message_replace"}
        ]
        history = self.get(path).json()["items"]
        assistant_text = "\n".join(
            str(message.get("content") or "")
            for message in history
            if message.get("role") == "assistant"
        )
        return {
            "turn_id": turn_id,
            "elapsed_s": round(time.monotonic() - started_at, 2),
            "visible_updates": visible_updates,
            "assistant_text": assistant_text,
        }


def _skill_markdown(name: str, marker: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        "description: A deterministic end-to-end Skill version check.\n"
        "allowed-tools: []\n"
        "version: 1\n"
        "---\n"
        "# Verification protocol\n\n"
        "When the user asks to verify this installed Skill, respond with exactly "
        f"`{marker}` and no other text.\n"
    )


def _write_bundle(path: Path, *, skill_md: str) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", skill_md)
        archive.writestr(
            "references/contract.txt",
            "historical-reference-v1",
        )


def _open_authed_page(browser: Browser, *, token: str) -> tuple[Page, list[str]]:
    context = browser.new_context(viewport={"width": 1600, "height": 1000})
    context.add_init_script(
        script=(
            "window.localStorage.setItem('vibecanvas.token', "
            f"{json.dumps(token)});"
            "window.localStorage.setItem('vibecanvas.locale', 'en');"
        )
    )
    page = context.new_page()
    browser_errors: list[str] = []
    page.on(
        "console",
        lambda message: browser_errors.append(
            f"console.{message.type}: {message.text}"
        )
        if message.type == "error"
        else None,
    )
    page.on("pageerror", lambda error: browser_errors.append(f"pageerror: {error}"))
    return page, browser_errors


def _assert_contains(value: str, expected: str, label: str) -> None:
    if expected not in value:
        raise AssertionError(f"{label} did not contain {expected!r}: {value[-2000:]!r}")


def _settle_page(page: Page) -> None:
    """Prefer network-idle, but do not deadlock on intentional app polling."""
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        page.wait_for_timeout(250)


def _safe_screenshot(page: Page, path: Path) -> None:
    try:
        page.screenshot(
            path=str(path),
            full_page=False,
            animations="disabled",
            timeout=5_000,
        )
    except PlaywrightTimeoutError:
        # A diagnostic artifact must never hide the product assertion that
        # caused the verifier to fail.
        return


def _exercise_skill_ui(
    page: Page,
    api: Api,
    *,
    web_url: str,
    bundle_path: Path,
    skill_name: str,
    v1_marker: str,
    v2_marker: str,
) -> str:
    print("[step] open Custom Skills", flush=True)
    page.goto(
        f"{web_url.rstrip('/')}/skills?tab=custom",
        wait_until="domcontentloaded",
    )
    page.get_by_role("heading", name="Skills").wait_for(state="visible")
    _settle_page(page)
    print("[step] upload ZIP package", flush=True)
    page.get_by_role("button", name="Upload Skill Package").first.click()
    page.locator('input[type="file"][accept*=".zip"]').set_input_files(bundle_path)
    with page.expect_response(
        lambda response: "/api/v1/skills/custom" in response.url,
        timeout=30_000,
    ) as response_info:
        page.get_by_role("button", name="Import Skill").click()
    upload_response = response_info.value
    if not upload_response.ok:
        raise AssertionError(
            "Skill upload failed: "
            f"{upload_response.status} {upload_response.text()[:2000]}"
        )
    page.get_by_role("dialog", name="Upload Skill Package").wait_for(state="hidden")

    card = page.get_by_test_id("skill-card").filter(has_text=skill_name)
    card.wait_for(state="visible")
    skill_link = card.get_by_role("link", name=skill_name)
    href = skill_link.get_attribute("href")
    if not href or not href.startswith("/skills/"):
        raise AssertionError(f"uploaded Skill card has invalid href: {href!r}")
    skill_id = href.rsplit("/", 1)[-1]
    skill_link.click()
    page.wait_for_url(f"**/skills/{skill_id}")
    page.get_by_role("heading", name=skill_name).wait_for(state="visible")
    _settle_page(page)

    print("[step] verify read-only v1 and save durable draft", flush=True)
    page.get_by_role("tab", name="Instructions").click()
    _assert_contains(page.locator("main").inner_text(), v1_marker, "published v1 UI")
    if page.locator('textarea[aria-label="SKILL.md"]').count() != 0:
        raise AssertionError("uploaded Custom Skill was editable before Edit was clicked")

    page.get_by_role("button", name="Edit").click()
    editor = page.locator('textarea[aria-label="SKILL.md"]')
    editor.wait_for(state="visible")
    editor.fill(_skill_markdown(skill_name, v2_marker))
    page.get_by_role("button", name="Save draft").click()
    page.get_by_text("Unpublished changes", exact=True).wait_for(state="visible")

    latest_before_publish = api.get(f"/api/v1/skills/{skill_id}").json()
    draft = api.get(f"/api/v1/skills/{skill_id}/draft").json()
    _assert_contains(
        latest_before_publish["skill_md"],
        v1_marker,
        "published head after draft save",
    )
    _assert_contains(draft["skill_md"], v2_marker, "durable draft")

    # A full document reload must reconstruct the editable working tree from
    # PostgreSQL, not from component memory or browser storage.
    print("[step] reload and resume draft", flush=True)
    page.reload(wait_until="domcontentloaded")
    _settle_page(page)
    editor = page.locator('textarea[aria-label="SKILL.md"]')
    editor.wait_for(state="visible")
    _assert_contains(editor.input_value(), v2_marker, "draft after browser reload")

    new_version = page.get_by_role("button", name="New version")
    new_version.wait_for(state="visible")
    if not new_version.is_enabled():
        raise AssertionError("New version remained disabled after durable draft reload")
    new_version.click()
    version_input = page.locator("#skill-version")
    if version_input.input_value() != "2":
        raise AssertionError(
            f"new version dialog defaulted to {version_input.input_value()!r}, not '2'"
        )
    page.get_by_role("button", name="Create version").click()
    page.get_by_text("Unpublished changes", exact=True).wait_for(state="hidden")
    page.locator('textarea[aria-label="SKILL.md"]').wait_for(state="hidden")

    latest = api.get(f"/api/v1/skills/{skill_id}").json()
    versions = api.get(f"/api/v1/skills/{skill_id}/versions").json()
    if latest["version"] != 2 or latest["has_draft"]:
        raise AssertionError(f"published v2 state is incorrect: {latest!r}")
    _assert_contains(latest["skill_md"], v2_marker, "published v2")
    if [revision["version"] for revision in versions] != [2, 1]:
        raise AssertionError(f"unexpected Skill version order: {versions!r}")

    print("[step] browse immutable v1 history", flush=True)
    version_select = page.get_by_role("combobox", name="Select version")
    version_select.click()
    page.get_by_role("option", name="v1", exact=True).click()
    page.get_by_text("Historical version", exact=True).wait_for(state="visible")
    page.get_by_role("tab", name="Instructions").click()
    _assert_contains(page.locator("main").inner_text(), v1_marker, "historical v1 UI")
    if page.get_by_role("button", name="Edit").count() != 0:
        raise AssertionError("historical Skill version exposed an Edit action")

    page.get_by_role("tab", name="Files").click()
    page.get_by_role("treeitem", name="contract.txt").locator("button").click()
    page.get_by_text("historical-reference-v1", exact=True).wait_for(state="visible")

    # The historical selection is encoded in the URL and must survive reload.
    revision_query = page.url
    if "revision=" not in revision_query:
        raise AssertionError(f"historical selection was not reflected in URL: {page.url}")
    print("[step] reload historical selection and return to latest", flush=True)
    page.reload(wait_until="domcontentloaded")
    _settle_page(page)
    page.get_by_text("Historical version", exact=True).wait_for(state="visible")
    if page.get_by_role("combobox", name="Select version").inner_text().strip() != "v1":
        raise AssertionError("historical version dropdown did not survive reload")

    version_select = page.get_by_role("combobox", name="Select version")
    version_select.click()
    page.get_by_role("option", name="Latest", exact=False).click()
    page.get_by_role("tab", name="Instructions").click()
    _assert_contains(page.locator("main").inner_text(), v2_marker, "latest v2 UI")
    return skill_id


def _exercise_runtimes(
    api: Api,
    *,
    skill_name: str,
    expected_marker: str,
) -> dict[str, Any]:
    original_runtime = api.get("/api/v1/agent-runtime/settings").json()[
        "default_runtime_type"
    ]
    scope_id = api.get("/api/v1/chats/bootstrap?surface=chat").json()[
        "carrier_scope_id"
    ]
    codex_chat = f"skill-codex-{uuid.uuid4().hex}"
    try:
        print("[step] verify published Skill in Codex Runtime", flush=True)
        api.put(
            "/api/v1/agent-runtime/settings",
            {"default_runtime_type": "codex"},
        )
        codex_capabilities = api.get("/api/v1/agent-runtime/capabilities").json()
        if not codex_capabilities.get("runtime_available"):
            raise AssertionError(
                f"Codex broker Runtime is unavailable: {codex_capabilities!r}"
            )
        codex_model = next(
            model
            for model in codex_capabilities["models"]
            if model["id"] == codex_capabilities["default_model_id"]
        )
        efforts = [
            item["id"] for item in codex_model.get("supported_reasoning_efforts") or []
        ]
        codex = api.stream_turn(
            scope_id=scope_id,
            chat_id=codex_chat,
            content=(
                f"${skill_name} Verify this installed Skill now. Follow its "
                "SKILL.md verification protocol exactly; do not guess."
            ),
            model_id=codex_capabilities["default_model_id"],
            reasoning_effort="low" if "low" in efforts else None,
        )
        _assert_contains(codex["assistant_text"], expected_marker, "Codex Skill result")

        chats = api.get(
            f"/api/v1/chat-scopes/{scope_id}/chats?surface=chat"
        ).json()["items"]
        bindings = {
            item["chat_id"]: item["runtime_type"]
            for item in chats
            if item["chat_id"] == codex_chat
        }
        if bindings != {codex_chat: "codex"}:
            raise AssertionError(f"incorrect Chat Runtime bindings: {bindings!r}")
        return {
            "codex_elapsed_s": codex["elapsed_s"],
            "codex_streamed": bool(codex["visible_updates"]),
            "bindings": bindings,
        }
    finally:
        api.delete(
            f"/api/v1/chat-scopes/{scope_id}/chats/{codex_chat}?surface=chat"
        )
        api.put(
            "/api/v1/agent-runtime/settings",
            {"default_runtime_type": original_runtime},
        )


def run(args: argparse.Namespace) -> None:
    api = Api(args.api_url)
    skill_id = ""
    browser: Browser | None = None
    skill_name = f"e2e-skill-{uuid.uuid4().hex[:8]}"
    v1_marker = "SKILL_VERSION_ONE_SHOULD_NOT_BE_USED"
    v2_marker = "SKILL_VERSION_TWO_OK"
    try:
        api.login()
        with tempfile.TemporaryDirectory(prefix="vibecanvas-skill-e2e-") as temp:
            bundle_path = Path(temp) / f"{skill_name}.zip"
            _write_bundle(
                bundle_path,
                skill_md=_skill_markdown(skill_name, v1_marker),
            )
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page, browser_errors = _open_authed_page(
                    browser,
                    token=api.token,
                )
                try:
                    skill_id = _exercise_skill_ui(
                        page,
                        api,
                        web_url=args.web_url,
                        bundle_path=bundle_path,
                        skill_name=skill_name,
                        v1_marker=v1_marker,
                        v2_marker=v2_marker,
                    )
                    _safe_screenshot(
                        page,
                        Path(args.artifact_dir) / "skill-version-e2e.png",
                    )
                except Exception:
                    if browser_errors:
                        print(
                            "[browser errors]\n" + "\n".join(browser_errors),
                            flush=True,
                        )
                    _safe_screenshot(
                        page,
                        Path(args.artifact_dir) / "skill-version-e2e-failed.png",
                    )
                    raise
                finally:
                    page.context.close()
                    browser.close()
                    browser = None

                if browser_errors:
                    raise AssertionError(
                        "browser emitted errors:\n" + "\n".join(browser_errors)
                    )

        runtime_result = (
            {}
            if args.skip_runtime
            else _exercise_runtimes(
                api,
                skill_name=skill_name,
                expected_marker=v2_marker,
            )
        )
        print(
            json.dumps(
                {
                    "skill_id": skill_id,
                    "ui_version_flow": "passed",
                    "runtime_flow": "skipped" if args.skip_runtime else "passed",
                    **runtime_result,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        if browser is not None:
            browser.close()
        if skill_id:
            api.delete(f"/api/v1/skills/{skill_id}")
        api.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--web-url", default="http://127.0.0.1:9001")
    parser.add_argument("--artifact-dir", default="/tmp")
    parser.add_argument(
        "--skip-runtime",
        action="store_true",
        help="Run only the browser-managed version workflow.",
    )
    run(parser.parse_args())


if __name__ == "__main__":
    main()
