"""Skill catalog installation and installed-bundle routes."""
from __future__ import annotations

import io
import uuid
import zipfile

import pytest
import pytest_asyncio

from vibecanvas_api.services.runtime_skills import hydrate_runtime_skills


SKILL_MD = (
    "---\nname: greet\ndescription: say hi\nallowed-tools: [bash]\n"
    "version: 1\n---\n# How\nsay hi nicely"
)


@pytest_asyncio.fixture
async def authed_client(client, monkeypatch):
    # This route suite tests publication, not the cross-loop Redis rate limiter.
    from unittest.mock import AsyncMock
    monkeypatch.setattr("vibecanvas_api.routes.auth._consume_unauthenticated_rate_limit", AsyncMock())
    email = f"skills-{uuid.uuid4().hex[:8]}@example.com"
    client.base_url = "https://flowork.top"
    client.headers['Origin'] = 'https://flowork.top'
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": "Test User", "password": "pw12345678"},
    )
    assert response.status_code == 201, response.text
    if response.json().get('session_token'):
        client.headers['Authorization'] = 'Bearer ' + response.json()['session_token']
    else:
        client.headers['X-CSRF-Token'] = next(c.value for c in client.cookies.jar if c.name.endswith('-csrf'))
    return client


@pytest.mark.asyncio
async def test_catalog_install_list_read_and_delete(authed_client, monkeypatch):
    async def fake_download(*, source, source_id):
        assert source == "openai" and source_id == "greet"
        return (
            {
                "source": "openai",
                "source_id": "greet",
                "name": "greet",
                "description": "say hi",
                "version": 1,
                "allowed_tools": ["bash"],
                "homepage": "https://github.com/openai/skills/tree/main/skills/.curated/greet",
                "revision": "abc123",
            },
            [
                ("SKILL.md", "text/markdown", SKILL_MD.encode()),
                ("references/example.txt", "text/plain", b"hello"),
            ],
        )

    monkeypatch.setattr(
        "vibecanvas_api.routes.skills.download_skill_bundle", fake_download
    )
    response = await authed_client.post(
        "/api/v1/skills/catalog/install",
        json={"source": "openai", "source_id": "greet"},
    )
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["source"] == "openai" and row["source_id"] == "greet"

    listed = (await authed_client.get("/api/v1/skills")).json()["items"]
    assert [item["name"] for item in listed] == ["greet"]

    detail = (await authed_client.get(f"/api/v1/skills/{row['id']}")).json()
    assert detail["skill_md"] == SKILL_MD
    assert "say hi nicely" in detail["body"]
    assert detail["files"] == ["SKILL.md", "references/example.txt"]
    file_response = await authed_client.get(
        f"/api/v1/skills/{row['id']}/files/references/example.txt"
    )
    assert file_response.text == "hello"
    assert (await authed_client.delete(f"/api/v1/skills/{row['id']}")).status_code == 204


@pytest.mark.asyncio
async def test_custom_skill_registration_is_not_exposed(authed_client):
    create = await authed_client.post("/api/v1/skills", json={"skill_md": SKILL_MD})
    assert create.status_code == 405
    update = await authed_client.put(
        f"/api/v1/skills/{uuid.uuid4()}", json={"skill_md": SKILL_MD}
    )
    assert update.status_code == 405


@pytest.mark.asyncio
async def test_custom_skill_draft_does_not_move_head_until_version_is_published(
    authed_client, tmp_path, monkeypatch, openfga_allow_all,
):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(openfga_allow_all, "close", AsyncMock(), raising=False)
    monkeypatch.setattr("vibecanvas_api.authorization.openfga_client.openfga_client_from_config", lambda: openfga_allow_all)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("SKILL.md", SKILL_MD)
        archive.writestr("references/example.txt", "reference")
    created = await authed_client.post(
        "/api/v1/skills/custom",
        files={"bundle": ("greet.zip", output.getvalue(), "application/zip")},
    )
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["source"] == "custom"
    assert len(row["revision_hash"]) == 64

    detail = (await authed_client.get(f"/api/v1/skills/{row['id']}")).json()
    assert detail["files"] == ["SKILL.md", "references/example.txt"]
    assert detail["has_draft"] is False

    draft_md = SKILL_MD.replace("say hi nicely", "say hello with the reference")
    saved = await authed_client.put(
        f"/api/v1/skills/{row['id']}/draft",
        json={"skill_md": draft_md, "expected_hash": row["revision_hash"]},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["has_changes"] is True
    assert saved.json()["files"] == ["SKILL.md", "references/example.txt"]

    # Saving the working tree must not change the published revision.
    published = (await authed_client.get(f"/api/v1/skills/{row['id']}")).json()
    assert published["revision_hash"] == row["revision_hash"]
    assert "say hi nicely" in published["skill_md"]
    assert published["has_draft"] is True

    identity = (await authed_client.get("/api/v1/auth/me")).json()
    runtime_mount = tmp_path / "runtime-skills"
    await hydrate_runtime_skills(
        destination=str(runtime_mount),
        tenant_id=identity["tenant_id"],
        user_id=identity["user_id"],
        skills=[{
            "skill_id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "revision_hash": row["revision_hash"],
            "root_path": f"/skills/{row['id']}",
            "allowed_tools": row["allowed_tools"],
        }],
    )
    published_root = runtime_mount / row["id"]
    assert "say hi nicely" in (published_root / "SKILL.md").read_text()
    assert not (runtime_mount / row["id"] / "drafts").exists()

    updated = await authed_client.post(
        f"/api/v1/skills/{row['id']}/versions",
        json={"version": 2, "expected_hash": saved.json()["draft_hash"]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2
    assert updated.json()["revision_hash"] != row["revision_hash"]
    detail = (await authed_client.get(f"/api/v1/skills/{row['id']}")).json()
    assert detail["files"] == ["SKILL.md", "references/example.txt"]
    assert detail["has_draft"] is False
    assert "say hello with the reference" in detail["skill_md"]
    assert "version: 2" in detail["skill_md"]

    versions = (
        await authed_client.get(f"/api/v1/skills/{row['id']}/versions")
    ).json()
    assert [item["version"] for item in versions] == [2, 1]
    assert versions[0]["is_latest"] is True
    historical = (
        await authed_client.get(
            f"/api/v1/skills/{row['id']}/versions/{versions[1]['revision_id']}"
        )
    ).json()
    assert historical["version"] == 1
    assert "say hi nicely" in historical["skill_md"]
    historical_file = await authed_client.get(
        f"/api/v1/skills/{row['id']}/versions/"
        f"{versions[1]['revision_id']}/files/references/example.txt"
    )
    assert historical_file.text == "reference"

    no_draft = await authed_client.post(
        f"/api/v1/skills/{row['id']}/versions",
        json={"version": 3, "expected_hash": "stale"},
    )
    assert no_draft.status_code == 409


@pytest.mark.asyncio
async def test_custom_skill_rejects_zip_traversal(authed_client):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../SKILL.md", SKILL_MD)
    response = await authed_client.post(
        "/api/v1/skills/custom",
        files={"bundle": ("unsafe.zip", output.getvalue(), "application/zip")},
    )
    assert response.status_code == 422
    assert "unsafe Skill bundle path" in response.text
