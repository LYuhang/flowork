"""Gate 2 — vibecanvas-api declares + uses vibecanvas-engine."""

from __future__ import annotations

from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[2]


def test_engine_imports_successfully():
    """The workflow engine must be installed and importable."""
    from vibecanvas_engine import Workflow
    assert Workflow.__module__ == "vibecanvas_engine.workflow"


def test_engine_declared_as_dependency():
    """pyproject.toml must declare vibecanvas-engine in [project.dependencies]."""
    # Source-tree test runs do not require the API wheel to be installed, so
    # inspect the authoritative project metadata directly.
    with (ROOT / "api" / "pyproject.toml").open("rb") as fh:
        req_strs = tomllib.load(fh)["project"]["dependencies"]
    assert any("vibecanvas-engine" in r for r in req_strs), (
        f"vibecanvas-engine not in api's declared dependencies: {req_strs}"
    )


def test_web_search_dependencies_declared_in_project_and_dev_snapshot():
    """The default HTML search provider requires Requests and BeautifulSoup."""
    pyproject = (ROOT / "api" / "pyproject.toml").read_text()
    assert '"requests>=' in pyproject
    assert '"beautifulsoup4>=' in pyproject

    dev_reqs = (ROOT / "requirements-dev.txt").read_text().splitlines()
    assert any(line.startswith("requests==") for line in dev_reqs)
    assert any(line.startswith("beautifulsoup4==") for line in dev_reqs)


def test_mcp_dependencies_declared_in_project_and_dev_snapshot():
    """MCP runtime deps must be present in both prod metadata and the dev snapshot."""
    pyproject = (ROOT / "api" / "pyproject.toml").read_text()
    assert '"mcp>=' in pyproject
    assert '"langchain-mcp-adapters>=' not in pyproject
    assert '"fastmcp' not in pyproject

    dev_reqs = (ROOT / "requirements-dev.txt").read_text().splitlines()
    assert any(line.startswith("mcp==") for line in dev_reqs)
    assert not any(line.startswith("langchain-mcp-adapters==") for line in dev_reqs)
    assert not any(line.startswith("fastmcp==") for line in dev_reqs)


def test_api_actually_uses_engine_workflow():
    """The migrated data plane should call into engine.Workflow somewhere."""
    from vibecanvas_api.services.platform_mcp.build_tools.workflow_file import Workflow

    assert Workflow.__module__ == "vibecanvas_engine.workflow"
