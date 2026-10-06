"""Diagram CLI and shared Preview structural/evidence contracts."""

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from urllib.parse import quote
import zlib

import pytest

from vibecanvas_api.drawio import inspect_drawio
from vibecanvas_api.diagram_runtime import operations
from vibecanvas_api.flowork_cli import cli
from vibecanvas_api.services.agent_runtime.cli_gateway import CliGateway

MODEL = '''<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="a" vertex="1" parent="1" value="A"><mxGeometry x="10" y="20" width="120" height="60" as="geometry"/></mxCell>
<mxCell id="b" vertex="1" parent="1" value="B"><mxGeometry x="200" y="20" width="120" height="60" as="geometry"/></mxCell>
<mxCell id="edge" edge="1" parent="1" source="a" target="b"><mxGeometry relative="1" as="geometry"/></mxCell>
</root></mxGraphModel>'''


def document(count=2, compressed=False):
    content = MODEL
    if compressed:
        compressor = zlib.compressobj(wbits=-15)
        content = base64.b64encode(compressor.compress(quote(MODEL).encode()) + compressor.flush()).decode()
    return ("<mxfile>" + "".join(f'<diagram id="p-{i}" name="Page {i}">{content}</diagram>' for i in range(1, count + 1)) + "</mxfile>").encode()


@pytest.mark.parametrize("compressed", [False, True])
def test_review_is_page_scoped_and_decodes_compressed(compressed):
    inspection = inspect_drawio(document(3, compressed))
    assert inspection.valid, inspection.issues
    assert inspection.cells == 15 and inspection.pages == 3
    assert inspection.vertices == 6 and inspection.edges == 3
    assert all(page["compressed"] is compressed for page in inspection.page_details)


@pytest.mark.parametrize("data,code", [
    (MODEL.replace('id="b"', 'id="a"').encode(), "duplicate-drawio-cell-id"),
    (MODEL.replace('target="b"', 'target="missing"').encode(), "dangling-drawio-terminal"),
    (MODEL.replace('parent="0"', 'parent="a"').encode(), "parent-cycle"),
    (MODEL.replace('x="10"', 'x="NaN"').encode(), "invalid-geometry"),
    (MODEL.replace('width="120"', 'width="-1"').encode(), "invalid-geometry"),
    (b'<mxfile/>', "invalid-drawio-xml"),
    (b'<mxfile><diagram>bad-base64</diagram></mxfile>', "invalid-drawio-xml"),
    (b'<mxGraphModel/>', "invalid-model-root"),
    (('<!DOCTYPE x [<!ENTITY y "bad">]>' + MODEL).encode('utf-16'), "unsafe-xml-declaration"),
])
def test_structural_errors(data, code):
    assert code in {item["code"] for item in inspect_drawio(data).issues}


def test_wrapped_cell_ids_and_missing_geometry_warning():
    data = b'<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><object id="node" label="Node"><mxCell vertex="1" parent="1"/></object><mxCell id="edge" edge="1" parent="1" source="node" target="node"/></root></mxGraphModel>'
    inspection = inspect_drawio(data)
    assert inspection.valid
    assert any(item["code"] == "missing-geometry" for item in inspection.warnings)


@pytest.mark.parametrize("args", [
    ["review"], ["review", "--file", "x.json"],
    ["render", "--file", "x.drawio", "--pages", "0"],
    ["render", "--file", "x.drawio", "--pages", "3-1"],
    ["render", "--file", "x.drawio", "--dpi", "144"],
    ["search-shapes", "--query", ""], ["search-shapes", "--query", "aws", "--limit", "51"],
])
def test_cli_invalid_args(args, capsys):
    assert cli.main(["diagram", *args]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_arguments"


@pytest.mark.parametrize("action", ["review", "render", "search-shapes"])
def test_help_is_discoverable(action, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["diagram", action, "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert ("--query" if action == "search-shapes" else "--file") in output
    if action == "render":
        assert "do NOT mkdir" in output


def test_missing_runtime_reports_the_actual_diagram_command(monkeypatch):
    monkeypatch.setattr(operations.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="flowork-diagram-search.*not executable"):
        operations.search_shapes("database")


@pytest.fixture
def fake_export(monkeypatch):
    calls = []
    monkeypatch.setattr(operations, "_required_command", lambda *_: "export")

    def run(args, **kwargs):
        assert "timeout" not in kwargs
        calls.append(args)
        Path(args[args.index("--output") + 1]).write_bytes(b"\x89PNG\r\n\x1a\n" + args[args.index("--page-index") + 1].encode())
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(operations.subprocess, "run", run)
    return calls


def test_render_all_and_selected_pages(tmp_path, fake_export):
    source = tmp_path / "diagram.drawio"
    source.write_bytes(document(23))
    original = source.read_bytes()
    progress = []
    result = operations.render_diagram(str(source), output_dir=str(tmp_path / "all"), progress=progress.append)
    assert result["status"] == "succeeded" and result["complete"]
    assert result["rendered_pages"] == result["total_pages"] == 23
    assert len(fake_export) == 23
    assert [args[args.index("--page-index") + 1] for args in fake_export] == list(map(str, range(1, 24)))
    selected = operations.render_diagram(str(source), pages="2,1-2,4", output_dir=str(tmp_path / "selected"))
    assert [image["page"] for image in selected["images"]] == [1, 2, 4]
    assert not selected["complete"]
    assert source.read_bytes() == original
    assert operations.render_diagram(str(source), pages="24")["error"] == "invalid_arguments"
    assert operations.render_diagram(str(source), output_dir=str(tmp_path / "all"))["error"] == "invalid_arguments"


def test_diagram_review_and_render_from_chat_cwd(tmp_path, monkeypatch, fake_export):
    from vibecanvas_api.document_runtime import rendering, review

    assert "/chats" in review._WORKSPACE_ROOTS
    chats = tmp_path / "chats"
    cwd = chats / "example-chat"
    cwd.mkdir(parents=True)
    for module in (review, rendering):
        monkeypatch.setattr(module, "_WORKSPACE_ROOTS", (str(chats),))
    monkeypatch.chdir(cwd)
    Path("diagram.drawio").write_bytes(document(1))
    assert operations.review_diagram("diagram.drawio")["status"] == "passed"
    result = operations.render_diagram("diagram.drawio", output_dir="preview")
    assert result["status"] == "succeeded" and result["complete"]
    assert result["output_dir"] == str(cwd / "preview")
    assert len(fake_export) == 1


def test_render_uses_source_snapshot_and_retains_partial(tmp_path, monkeypatch, fake_export):
    source = tmp_path / "diagram.drawio"
    data = document()
    source.write_bytes(data)
    original = operations.subprocess.run

    def run(args, **kwargs):
        assert Path(args[-1]).read_bytes() == data
        if args[args.index("--page-index") + 1] == "2":
            return SimpleNamespace(returncode=1, stdout="", stderr="Page two failed.")
        return original(args, **kwargs)

    monkeypatch.setattr(operations.subprocess, "run", run)
    result = operations.render_diagram(str(source), output_dir=str(tmp_path / "images"), progress=lambda _: source.write_bytes(b"changed"))
    assert result["source_hash"] == "sha256:" + hashlib.sha256(data).hexdigest()
    assert result["status"] == "failed" and result["rendered_pages"] == 1
    assert Path(result["images"][0]["file"]).is_file()


@pytest.mark.asyncio
async def test_gateway_diagram_worker_and_standalone_launcher(tmp_path):
    source = tmp_path / "diagram.drawio"
    source.write_bytes(document(compressed=True))
    gateway = CliGateway()

    async def no_host(*_):
        raise AssertionError("Diagram must execute locally.")

    env = await gateway.activate(no_host)
    try:
        result = await asyncio.to_thread(cli.request, env["FLOWORK_CLI_SOCKET"], {"file": str(source)}, operation="diagram.review")
        assert result["status"] == "passed"
        assert result["details"]["page_count"] == 2
        assert (Path(env["FLOWORK_CLI_SOCKET"]).parent / "diagram_cli.py").is_file()
    finally:
        await gateway.close()


@pytest.mark.parametrize("query,expected", [("database", 1), ("zzzyyyxxxqqq", 0)])
def test_official_search_library_without_mcp(tmp_path, query, expected):
    root = Path(__file__).resolve().parents[2]
    helper = root / "api/drawio-runtime/search.mjs"
    if not shutil.which("node") or not (helper.parent / "node_modules/@drawio/mcp").exists():
        pytest.skip("Pinned Node search runtime is not installed.")
    index = tmp_path / "index.json"
    index.write_text(json.dumps([{"title": "Database", "style": "shape=cylinder;", "tags": "database storage", "w": 80, "h": 100}]))
    env = {**os.environ, "DRAWIO_SHAPE_INDEX_PATH": str(index), "DRAWIO_ICON_SERVICE_URL": "off"}
    result = subprocess.run(["node", str(helper)], input=json.dumps({"query": query, "limit": 2}), text=True, capture_output=True, env=env, timeout=15)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["status"] == "succeeded"
    assert len(response["shapes"]) == expected
    if expected:
        assert response["shapes"][0] == {"title": "Database", "style": "shape=cylinder;", "width": 80, "height": 100}
    index.write_text("[]")
    failed = subprocess.run(["node", str(helper)], input=json.dumps({"query": query}), text=True, capture_output=True, env=env, timeout=15)
    assert failed.returncode == 1
    assert json.loads(failed.stdout)["error"] == "shape_search_failed"


def test_installed_desktop_renders_distinct_pages_and_selection(tmp_path):
    if not shutil.which("flowork-drawio-export"):
        pytest.skip("Official draw.io Desktop runtime is not installed.")
    from PIL import Image, ImageChops
    source = tmp_path / "pages.drawio"
    source.write_bytes(b'<mxfile><diagram id="one" name="One">' + MODEL.encode() + b'</diagram><diagram id="two" name="Two">' + MODEL.replace('x="200"', 'x="400"').encode() + b'</diagram></mxfile>')
    result = operations.render_diagram(str(source), output_dir=str(tmp_path / "all"))
    assert result["status"] == "succeeded", result
    assert result["rendered_pages"] == 2
    first, second = [Image.open(image["file"]).convert("RGBA") for image in result["images"]]
    assert first.size != second.size
    selected = operations.render_diagram(str(source), pages="2", output_dir=str(tmp_path / "selected"))
    assert selected["status"] == "succeeded", selected
    assert selected["images"][0]["page"] == 2 and not selected["complete"]
    with Image.open(selected["images"][0]["file"]) as image:
        assert image.size == second.size
        assert ImageChops.difference(image.convert("RGBA"), second).getbbox() is None
