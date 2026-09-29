"""Small inline previews must still expose the original file download."""
from types import SimpleNamespace

import pytest

from vibecanvas_api.routes import previews
from vibecanvas_api.schemas.preview import ProjectFileRefV1


@pytest.mark.parametrize(
    ("name", "mime", "source", "renderer"),
    [
        ("orders.csv", "text/csv", "title,amount\r\nBook,51.77\r\n", "spreadsheet"),
        ("report.json", "application/json", '{"total":309.55}', "text"),
        ("report.html", "text/html", "<h1>Report</h1>", "html"),
        ("notes.md", "text/markdown", "# Report\n", "markdown"),
    ],
)
def test_inline_preview_keeps_original_download_url(monkeypatch, name, mime, source, renderer):
    original_bytes = b"\xef\xbb\xbf" + source.encode()
    resolved = previews._ResolvedFile(
        file_ref=ProjectFileRefV1(
            schemaVersion=1, scope="project", projectId="project", path=f"/data/{name}",
        ),
        row=SimpleNamespace(content_type=mime, size_bytes=len(original_bytes), content_revision="v1"),
        scope_id="workspace",
        run_id="",
    )
    auth = SimpleNamespace(tenant_id="tenant")
    calls = []

    def signed_original(**kwargs):
        calls.append(kwargs)
        return "/api/v1/vfs/raw?signature=test-original"

    monkeypatch.setattr(previews, "_safe_inline_url", signed_original)
    descriptor = previews._descriptor(resolved=resolved, auth=auth, data=original_bytes)

    assert descriptor.load_policy == "inline"
    assert descriptor.renderer == renderer
    assert descriptor.content.inline_text == source
    assert descriptor.text.bom is True
    assert descriptor.capabilities.download is True
    # Download must use the original bytes, not a re-encoded inline string
    # that has already lost the BOM (and may later be edited in the UI).
    assert descriptor.content.url == "/api/v1/vfs/raw?signature=test-original"
    assert calls == [{"resolved": resolved, "auth": auth}]
