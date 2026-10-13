"""Web draft/publication contracts with real Git, HTTP and PostgreSQL storage."""
import asyncio
import base64
import io
import zipfile
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from vibecanvas_api.flowork_cli import knowledge_cli as git
from vibecanvas_api.services.package_git import commit_web_package
from tests.test_workflow_authorization_integration import _browser_sessions  # noqa: F401


def unpack(root, files, entry):
    root.mkdir()
    git.materialize(root, [{"path": p, "data": base64.b64encode(b).decode()} for p, _, b in files], entrypoint=entry)
    return git.require_clean_package(root)


@pytest.mark.parametrize('existing_git', [False, True])
def test_publish_add_edit_delete_and_preserve_previous_commit(tmp_path, existing_git):
    base = [('README.md', None, b'original\n'), ('remove.txt', None, b'remove')]
    if existing_git:
        base = commit_web_package(base, base, message='Initial publication')
    draft = [('README.md', None, b'changed\n'), ('new.bin', None, bytes(range(256))),
             ('.gitignore', None, b'new.bin\n')]
    published = commit_web_package(base, draft, message='Publish version 2')
    root = tmp_path / 'published'
    unpack(root, published, 'README.md')
    assert git.package_git(root, 'show', 'HEAD~1:README.md') == 'original\n'
    assert git.package_git(root, 'show', 'HEAD~1:remove.txt') == 'remove'
    assert not (root / 'remove.txt').exists()
    assert (root / 'new.bin').read_bytes() == bytes(range(256))
    assert git.package_git(root, 'log', '-1', '--format=%s').strip() == 'Publish version 2'
    assert dict((p, b) for p, _, b in base)['README.md'] == b'original\n'


def test_git_attribute_conversion_is_not_silently_published():
    base = [('README.md', None, b'original\n')]
    draft = [('README.md', None, b'changed\r\n'), ('.gitattributes', None, b'*.md text eol=lf\n')]
    with pytest.raises(ValueError, match='file bytes'):
        commit_web_package(base, draft, message='Publish')


@pytest.mark.asyncio
@pytest.mark.parametrize('resource,entry', [('skills', 'SKILL.md'), ('kb', 'README.md')])
async def test_web_draft_lifecycle_and_git_history(pg_engine, monkeypatch, tmp_path, resource, entry):
    from vibecanvas_api.app import build_app
    from vibecanvas_api.authorization import openfga_client
    from vibecanvas_api.routes import kb, skills
    from tests.test_skill_sharing_integration import SkillStore
    from tests.test_workflow_authorization_integration import _register_exact

    app = build_app()
    from tests.test_knowledge_base_authorization_integration import _RelationshipStore
    app.state.openfga_client = SkillStore() if resource == 'skills' else _RelationshipStore()
    monkeypatch.setattr(openfga_client, 'openfga_client_from_config', lambda: app.state.openfga_client)
    monkeypatch.setattr(kb, 'enqueue_package_indexing', AsyncMock())
    initial = b'---\nname: web-git-test\ndescription: Test\nversion: 1\n---\nOriginal\n'
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        headers, _, _ = await _register_exact(client, 'web_git_' + resource)
        if resource == 'skills':
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr(entry, initial)
            response = await client.post('/api/v1/skills/custom', files={'bundle': ('test.zip', archive.getvalue(), 'application/zip')}, headers=headers)
        else:
            response = await client.post('/api/v1/kb', json={'name': 'web-git-test'}, headers=headers)
        assert response.status_code == 201, response.text
        path = f'/api/v1/{resource}/{response.json()["id"]}'
        async def state():
            r = await client.get(path + '/draft', headers=headers)
            assert r.status_code == 200, r.text
            return r.json()
        def token(d):
            return (d.get('draft_hash') or d['base_revision_hash']) if resource == 'skills' else d['content_hash']
        async def edit(name, content, hash_, create=False):
            return await client.put(path + '/draft/files/' + name, data={'expected_hash': hash_, 'create': str(create).lower()}, files={'file': (name, content, 'text/plain')}, headers=headers)
        async def publish(hash_, version):
            return await client.post(path + '/versions', json={'expected_hash': hash_, **({'version': version} if resource == 'skills' else {})}, headers=headers)
        async def download(version, root):
            if resource == 'skills':
                detail = (await client.get(path, headers=headers)).json()
                paths = detail['files']
                url = path + '/files/'
            else:
                detail = (await client.get(path + f'/versions/{version}', headers=headers)).json()
                paths = detail['files']
                url = path + f'/versions/{version}/files/'
            files = []
            for p in paths:
                r = await client.get(url + p, headers=headers)
                assert r.status_code == 200, r.text
                files.append((p, None, r.content))
            return unpack(root, files, entry)

        original = await state()
        assert not original['has_changes']
        first = await edit('notes.txt', b'first draft', token(original), True)
        assert first.status_code == 200, first.text
        stale_hash = token(first.json())
        second = await edit('notes.txt', b'second draft', stale_hash)
        assert second.status_code == 200, second.text
        current_hash = token(second.json())
        assert (await publish(stale_hash, 2)).status_code == 409
        assert (await client.delete(path + '/draft', params={'expected_hash': stale_hash}, headers=headers)).status_code == 409
        assert token(await state()) == current_hash
        for name in ['.git/config', '.git/HEAD']:
            assert (await edit(name, b'bad', current_hash, True)).status_code == 422
            assert (await client.delete(path + '/draft/files/' + name, params={'expected_hash': current_hash}, headers=headers)).status_code == 422
        # A Git failure must leave the draft and the published version intact.
        module = skills if resource == 'skills' else kb
        real_commit = module.commit_web_package
        def fail(*args, **kwargs):
            raise ValueError('Git publication unavailable')
        monkeypatch.setattr(module, 'commit_web_package', fail)
        failed = await publish(current_hash, 2)
        assert failed.status_code == 422 and 'Git publication unavailable' in failed.text
        assert token(await state()) == current_hash
        monkeypatch.setattr(module, 'commit_web_package', real_commit)
        discarded = await client.delete(path + '/draft', params={'expected_hash': current_hash}, headers=headers)
        assert discarded.status_code == 200, discarded.text
        assert not discarded.json()['has_changes'] and 'notes.txt' not in discarded.json()['files']
        assert token(discarded.json()) == token(original)
        saved = await edit('notes.txt', b'published', token(discarded.json()), True)
        assert saved.status_code == 200, saved.text
        saved_hash = token(saved.json())
        if resource == 'kb':
            enqueue = kb.enqueue_package_indexing
            monkeypatch.setattr(kb, 'enqueue_package_indexing', AsyncMock(side_effect=RuntimeError('queue unavailable')))
            with pytest.raises(RuntimeError, match='queue unavailable'):
                await publish(saved_hash, 2)
            assert token(await state()) == saved_hash
            assert (await client.get(path, headers=headers)).json()['package_version'] == 1
            monkeypatch.setattr(kb, 'enqueue_package_indexing', enqueue)
        published = await publish(saved_hash, 2)
        assert published.status_code == 200, published.text
        assert not (await state())['has_changes']
        first_head = await download(2, tmp_path / 'v2')
        assert git.package_git(tmp_path / 'v2', 'rev-list', '--count', 'HEAD').strip() == '2'
        assert (await publish(saved_hash, 3)).status_code == 409
        d = await state()
        edited = await edit('notes.txt', b'next version', token(d))
        assert edited.status_code == 200, edited.text
        if resource == 'kb':
            edited = await edit(entry, b'# Plain README from the editor\n', token(edited.json()))
            assert edited.status_code == 200, edited.text
        # Saving retains the existing HEAD; only publication advances it.
        head = await client.get(path + '/draft/files/.git/refs/heads/main', headers=headers) if resource == 'skills' else await client.get(path + '/versions/0/files/.git/refs/heads/main', headers=headers)
        assert head.status_code == 200 and head.text.strip() == first_head
        concurrent = await asyncio.gather(publish(token(edited.json()), 3), publish(token(edited.json()), 3))
        assert sorted(r.status_code for r in concurrent) == [200, 409], [r.text for r in concurrent]
        await download(3, tmp_path / 'v3')
        assert git.package_git(tmp_path / 'v3', 'rev-parse', 'HEAD~1').strip() == first_head
        assert git.package_git(tmp_path / 'v3', 'show', 'HEAD~1:notes.txt') == 'published'
        assert (tmp_path / 'v3' / 'notes.txt').read_bytes() == b'next version'
