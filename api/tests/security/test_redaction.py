from __future__ import annotations

import json
import logging

from vibecanvas_api.observability.logging import configure_logging
from vibecanvas_api.security.redaction import REDACTED, redact_text, redact_value


def test_recursive_redaction_does_not_mutate_source():
    source = {
        "api_key": "top-secret",
        "nested": {"messages": [{"role": "user", "content": "private"}]},
        "outputs": {"customer_record": "private workflow result"},
        "errors": {"node": "private input was invalid"},
        "exception": "ValueError: private input was invalid",
        "safe": "request completed",
    }
    result = redact_value(source)
    assert result == {
        "api_key": REDACTED,
        "nested": {"messages": REDACTED},
        "outputs": REDACTED,
        "errors": REDACTED,
        "exception": REDACTED,
        "safe": "request completed",
    }
    assert source["api_key"] == "top-secret"


def test_embedded_credentials_are_redacted():
    text = (
        "Authorization: Bearer abc.def and "
        "https://example.test/a?token=hello&x=1 password=hunter2"
    )
    result = redact_text(text)
    assert "abc.def" not in result
    assert "hello" not in result
    assert "hunter2" not in result
    assert result.count(REDACTED) == 3


def test_stdlib_logging_redacts_before_json_render(capsys):
    configure_logging(force_format="json")
    logging.getLogger("vibecanvas_api.security.test").warning(
        "call failed api_key=%s", "do-not-log"
    )
    row = json.loads(capsys.readouterr().out.strip())
    rendered = json.dumps(row)
    assert "do-not-log" not in rendered
    assert REDACTED in rendered


def test_public_share_capabilities_are_removed_from_log_urls():
    token = "a" * 43
    for path in ("/prefix/share/", "/api/v1/public/chat-shares/"):
        assert token not in redact_text(f'GET https://example.org{path}{token} HTTP/1.1')
