"""Use real Git repositories to exercise the package CLI publication boundary."""
import base64
import json
from uuid import uuid4

import pytest

from vibecanvas_api.flowork_cli import cli, knowledge_cli as packages


def commit(root, message="Reviewed changes"):
    packages.package_git(root, "add", "--all")
    packages.package_git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com",
                         "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", message)


@pytest.mark.parametrize("resource,entrypoint", [("skill", "SKILL.md"), ("knowledge", "README.md")])
@pytest.mark.parametrize("state", ["missing_git", "untracked", "unstaged", "staged", "ignored", "conflict"])
def test_dirty_publication_never_dispatches(tmp_path, monkeypatch, capsys, resource, entrypoint, state):
    (tmp_path / entrypoint).write_text("original\n")
    if state != "missing_git":
        packages.prepare_download_git(tmp_path)
    if state == "untracked":
        (tmp_path / "new.txt").write_text("untracked")
    elif state in {"unstaged", "staged"}:
        (tmp_path / entrypoint).write_text("changed\n")
        if state == "staged":
            packages.package_git(tmp_path, "add", entrypoint)
    elif state == "ignored":
        (tmp_path / ".gitignore").write_text("secret.txt\n")
        commit(tmp_path)
        (tmp_path / "secret.txt").write_text("must not upload")
    elif state == "conflict":
        packages.package_git(tmp_path, "checkout", "-b", "other")
        (tmp_path / entrypoint).write_text("other\n")
        commit(tmp_path)
        packages.package_git(tmp_path, "checkout", "main")
        (tmp_path / entrypoint).write_text("main\n")
        commit(tmp_path)
        with pytest.raises(ValueError):
            packages.package_git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.com", "merge", "other")
        assert packages.package_git(tmp_path, "ls-files", "--unmerged")
    monkeypatch.setattr(cli, "request", lambda *a, **kw: pytest.fail("dirty package dispatched"))
    assert cli.main([resource, "publish", "--" + resource + "-id", str(uuid4()),
                     "--expected-version", "1", "--source-dir", str(tmp_path)], socket_path="test") == 2
    result = json.loads(capsys.readouterr().out)
    assert result["command_status"] == "failed"
    assert "git" in result["message"].lower()
    assert "git status" in result["hint"] and "Downloads" not in result["hint"]


@pytest.mark.parametrize("resource,entrypoint", [("skill", "SKILL.md"), ("knowledge", "README.md")])
def test_download_edit_commit_publish_download_preserves_history(tmp_path, monkeypatch, capsys, resource, entrypoint):
    stored = [{"path": entrypoint, "data": base64.b64encode(b"original\n").decode()}]
    identifier = str(uuid4())
    version = 1

    def request(endpoint, args, *, operation):
        nonlocal stored, version
        if operation.endswith("download"):
            return {"files": list(stored), "package_version": version}
        assert args["expected_version"] == version
        stored = args["files"]
        version += 1
        return {"package_version": version}

    monkeypatch.setattr(cli, "request", request)
    source = tmp_path / "first"
    command = [resource, "download", "--" + resource + "-id", identifier, "--output-dir"]
    assert cli.main(command + [str(source)], socket_path="test") == 0
    initial = json.loads(capsys.readouterr().out)
    assert initial["git_initialized"] and initial["git_clean"]
    (source / entrypoint).write_text("reviewed update\n")
    (source / "asset.bin").write_bytes(bytes(range(256)))
    commit(source)
    head = packages.require_clean_package(source)
    assert cli.main([resource, "publish", "--" + resource + "-id", identifier,
                     "--source-dir", str(source), "--expected-version", "1"], socket_path="test") == 0
    capsys.readouterr()
    restored = tmp_path / "restored"
    assert cli.main(command + [str(restored)], socket_path="test") == 0
    downloaded = json.loads(capsys.readouterr().out)
    assert not downloaded["git_initialized"] and downloaded["git_clean"]
    assert downloaded["git_commit"] == head
    assert packages.package_git(restored, "rev-list", "--count", "HEAD").strip() == "2"
    assert packages.package_git(restored, "show", "HEAD~1:" + entrypoint) == "original\n"
    assert (restored / "asset.bin").read_bytes() == bytes(range(256))
    assert any(item["path"].startswith(".git/objects/") for item in stored)


def test_parent_repository_and_linked_worktree_are_not_packages(tmp_path):
    (tmp_path / "README.md").write_text("parent")
    packages.prepare_download_git(tmp_path)
    child = tmp_path / "child"
    child.mkdir()
    with pytest.raises(ValueError, match="own .git directory"):
        packages.require_clean_package(child)
    (child / ".git").write_text("gitdir: ../.git")
    with pytest.raises(ValueError, match="own .git directory"):
        packages.require_clean_package(child)


def test_download_does_not_execute_git_filters_or_hooks(tmp_path):
    (tmp_path / "README.md").write_text("example")
    packages.prepare_download_git(tmp_path)
    packages.package_git(tmp_path, "config", "filter.payload.clean", "touch SHOULD_NOT_EXIST")
    (tmp_path / ".gitattributes").write_text("* filter=payload\n")
    with pytest.raises(ValueError, match="Non-portable"):
        packages.prepare_download_git(tmp_path)
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()


def test_existing_history_is_not_silently_committed_after_ui_edits(tmp_path):
    (tmp_path / "README.md").write_text("initial")
    before = packages.prepare_download_git(tmp_path)
    (tmp_path / "README.md").write_text("edited in UI")
    after = packages.prepare_download_git(tmp_path)
    assert not after["git_clean"]
    assert after["git_commit"] == before["git_commit"]


@pytest.mark.parametrize("resource", ["skill", "knowledge"])
def test_leaf_help_explains_git_and_platform_versions(capsys, resource):
    with pytest.raises(SystemExit) as exited:
        cli.main([resource, "publish", "--help"])
    assert exited.value.code == 0
    help_text = capsys.readouterr().out
    assert ".git" in help_text and "git log" in help_text
    assert "untracked" in help_text and "--expected-version" in help_text


@pytest.mark.asyncio
@pytest.mark.parametrize("resource,entrypoint", [("skill", "SKILL.md"), ("knowledge", "README.md")])
async def test_git_history_survives_encrypted_database_storage(pg_engine, tmp_path, resource, entrypoint):
    from tests.test_skills_repo import _seed_tenant_and_user
    from vibecanvas_api.storage.db import session_scope
    from vibecanvas_api.storage.repo_skills import SkillsRepo
    from vibecanvas_api.storage.repo_kb import KbRepo
    from vibecanvas_api.services.knowledge_packages import PackageFile, replace_package, package_snapshot
    from vibecanvas_api.services.agent_runtime.cli_knowledge import index_summary

    tenant, user = uuid4(), uuid4()
    await _seed_tenant_and_user(pg_engine, tenant, user)
    source = tmp_path / "source"
    source.mkdir()
    (source / entrypoint).write_text("---\nname: git-package\ndescription: Test\nversion: 1\n---\nFirst version\n")
    packages.prepare_download_git(source)
    (source / "notes.txt").write_text("second commit\n")
    commit(source)
    head = packages.require_clean_package(source)
    captured = [(item["path"], None, base64.b64decode(item["data"])) for item in packages.collect(source, entrypoint=entrypoint)]
    async with session_scope(tenant_id=str(tenant), user_id=str(user)) as session:
        if resource == "skill":
            repo = SkillsRepo(session)
            identifier = await repo.insert(tenant_id=str(tenant), user_id=str(user), name="git-package",
                                           description="Test", version=1, source="custom", files=captured)
        else:
            kb = await KbRepo(session).create_kb(tenant_id=tenant, user_id=user, name="git-package")
            identifier = kb.id
            await replace_package(session, kb_id=identifier, actor_user_id=user, expected_version=1,
                                  increment_version=False, files=[PackageFile(path, data, "") for path, _, data in captured])
    async with session_scope(tenant_id=str(tenant), user_id=str(user)) as session:
        if resource == "skill":
            repo = SkillsRepo(session)
            row = await repo.get(identifier)
            restored = await repo.read_revision_files(identifier, row["current_revision_id"])
            from vibecanvas_api.storage.repo_skills import skill_scope_id
            assert await repo.vfs.read_bytes(wf_id=skill_scope_id(user), path=f"/skills/{identifier}/SKILL.md")
            assert await repo.vfs.read_bytes(wf_id=skill_scope_id(user), path=f"/skills/{identifier}/.git/HEAD") is None
        else:
            restored = [(file.path, file.content_type, file.data) for file in await package_snapshot(session, identifier)]
            states = await KbRepo(session).list_files(identifier)
            git_states = [file for file in states if file.name.startswith(".git/")]
            assert git_states and all(file.status == "stored" and file.parser_type == "binary" for file in git_states)
            assert index_summary(states)["index_counts"]["stored"] == 0
    destination = tmp_path / "restored"
    destination.mkdir()
    packages.materialize(destination, [{"path": path, "data": base64.b64encode(data).decode()} for path, _, data in restored], entrypoint=entrypoint)
    result = packages.prepare_download_git(destination)
    assert result["git_clean"] and result["git_commit"] == head
    assert packages.package_git(destination, "rev-list", "--count", "HEAD").strip() == "2"
