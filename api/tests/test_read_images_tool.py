import base64
import io

import pytest
from PIL import Image

from vibecanvas_api.agents.tools.media.read_images import _do_read_images


@pytest.fixture
def image_workspace(tmp_path, monkeypatch):
    from vibecanvas_api.agents.tools import workspace_fs

    monkeypatch.delenv("VIBECANVAS_AGENT_RUNTIME_IN_SANDBOX", raising=False)
    monkeypatch.setattr(workspace_fs, "_WORKSPACE_ROOTS", (str(tmp_path),))
    path = tmp_path / "pixel.png"
    Image.new("RGB", (16, 12), color="red").save(path)
    return path


def _text(content):
    if isinstance(content, str):
        return content
    return "\n".join(block.get("text", "") for block in content)


@pytest.mark.asyncio
async def test_read_images_rejects_http_urls_with_actionable_error():
    content, artifact = await _do_read_images(
        ["https://example.com/image.png"],
    )
    assert artifact["status"] == "error"
    assert artifact["error"]["code"] == "no_images"
    assert "URLs are not supported" in content
    assert "Save the image to a local path" in content
    assert "accessible in the current environment" in content


@pytest.mark.asyncio
async def test_read_images_returns_pixels_and_partial_errors(image_workspace):
    content, artifact = await _do_read_images(
        [str(image_workspace), "https://example.com/image.png"],
    )
    assert artifact["status"] == "success"
    assert "Loaded 1 image(s)" in _text(content)
    assert "URLs are not supported" in _text(content)
    image = next(block for block in content if block["type"] == "image")
    assert base64.b64decode(image["base64"]) == image_workspace.read_bytes()
    assert image["mime_type"] == "image/png"
    assert artifact["loaded"][0]["width"] == 16
    assert artifact["loaded"][0]["height"] == 12
    assert "base64" not in str(artifact)


@pytest.mark.asyncio
async def test_read_images_validates_pixels_not_filename(image_workspace):
    image_workspace.write_text("not an image")
    content, artifact = await _do_read_images(
        [str(image_workspace)],
    )
    assert artifact["status"] == "error"
    assert "No images loaded" in content


@pytest.mark.asyncio
async def test_read_images_rejects_symlink_escape(image_workspace, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    from unittest.mock import patch
    link = allowed / "outside.png"
    link.symlink_to(image_workspace)
    with patch("vibecanvas_api.agents.tools.workspace_fs._WORKSPACE_ROOTS", (str(allowed),)):
        content, artifact = await _do_read_images([str(link)])
    assert artifact["status"] == "error"
    assert "path_outside_roots" in content


@pytest.mark.asyncio
async def test_read_images_normalizes_tiff(image_workspace):
    path = image_workspace.with_suffix(".tiff")
    Image.new("RGB", (13, 17), color="blue").save(path)
    content, artifact = await _do_read_images([str(path)])
    assert artifact["status"] == "success"
    block = next(block for block in content if block["type"] == "image")
    assert block["mime_type"] == "image/png"
    with Image.open(io.BytesIO(base64.b64decode(block["base64"]))) as image:
        assert image.format == "PNG"
        assert image.size == (13, 17)


@pytest.mark.asyncio
@pytest.mark.parametrize("paths", [[], ["/data/image.png"] * 9])
async def test_read_images_invalid_path_count(paths):
    content, artifact = await _do_read_images(paths)
    assert artifact["status"] == "error"
    assert artifact["error"]["code"] == "invalid_paths"
    assert "Pass between 1 and 8" in content
