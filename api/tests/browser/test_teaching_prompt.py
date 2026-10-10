"""Contract tests for the CLI-backed /browser workflow guidance."""

from vibecanvas_api.agents.commands import COMMAND_MODES, command_context_for
from vibecanvas_api.agents.prompts.compose import build_system_prompt


def test_browser_prompt_teaches_cli_observe_act_verify() -> None:
    prompt = command_context_for("browser")
    for instruction in (
        "## Browser mode", "flowork-cli browser", "Observe → act → verify",
        "tab-list", "snapshot --tab-id ID", "leaf --help", "fresh refs",
        "goto --tab-id ID --url URL", "not `navigate`", "browser --help",
        "CSS IDs are page-specific", "destination snapshot's fresh ref",
        "run-code", "eval", "dialog-accept", "cloud sandbox",
        "download --ref/--locator", "download --url",
        "browser-managed", "page.waitForEvent('download') before the trigger",
        "download.saveAs",
        "byte count, hash and persistence", "site-specific Cookie permission",
        "Agent code cannot grant", "private temporary", "revocation or turn end",
        "case-sensitive option", "differ from snapshot labels",
    ):
        assert instruction in prompt


def test_browser_command_exposes_no_mcp_tools_or_legacy_instructions() -> None:
    assert COMMAND_MODES["browser"].tools == []
    prompt = command_context_for("browser")
    for retired in (
        "browser_start_session", "browser_snapshot", "browser_file_upload",
        "browser_navigate", "official Playwright MCP",
        "unrestricted upstream evaluate/run-code tools are intentionally unavailable",
    ):
        assert retired not in prompt
    assert "There is no Browser MCP" in prompt
    assert "Every page operation names --tab-id" in prompt


def test_browser_prompt_preserves_partial_effects_and_safety() -> None:
    prompt = command_context_for("browser")
    for instruction in (
        "status=unknown", "does not undo earlier actions",
        "not a reason to repeat", "must not trigger another",
        "Never adopt another Chat/browser", "login, CAPTCHA",
        "Never print, preview, share or copy credentials",
        "poll that same session", "ending\nthe Agent turn cancels",
    ):
        assert instruction in prompt


def test_chat_prompt_has_no_browser_section() -> None:
    assert "## Browser mode" not in build_system_prompt(set())


def test_active_browser_does_not_change_base_system_prompt() -> None:
    assert build_system_prompt({"browser"}) == build_system_prompt(set())
