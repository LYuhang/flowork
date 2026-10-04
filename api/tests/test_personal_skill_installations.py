"""Installation choices are private even when actors share a package/workspace."""
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.test_repo_org import _seed_tenant_user_and_backfill
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_skill_installations import SkillInstallationsRepo


@pytest.mark.asyncio
async def test_installation_is_actor_private_and_uninstall_preserves_package(pg_engine):
    owner_org, owner = await _seed_tenant_user_and_backfill(pg_engine)
    recipient_org, recipient = await _seed_tenant_user_and_backfill(pg_engine)
    skill = uuid.uuid4()
    async with pg_engine.begin() as conn:
        await conn.execute(text("""INSERT INTO skills
            (skill_id, tenant_id, user_id, name, source)
            VALUES (:skill, :org, :owner, 'Installation QA', 'custom')"""),
            {"skill": skill, "org": owner_org, "owner": owner})
    async with session_scope(tenant_id=str(owner_org), user_id=str(owner)) as session:
        await SkillInstallationsRepo(session).install(owner, skill)
    async with session_scope(tenant_id=str(recipient_org), user_id=str(recipient)) as session:
        repo = SkillInstallationsRepo(session)
        assert await repo.installed_ids(recipient) == set()
        assert await repo.installed_ids(owner) == set()
        await repo.install(recipient, skill)
        await repo.install(recipient, skill)
        assert await repo.installed_ids(recipient) == {str(skill)}
        await repo.uninstall(owner, skill)
    # Owner-scope admission must not change the actor's personal selection.
    async with session_scope(tenant_id=str(owner_org), user_id=str(recipient)) as session:
        repo = SkillInstallationsRepo(session)
        assert await repo.installed_ids(recipient) == {str(skill)}
        await repo.uninstall(recipient, skill)
        assert await repo.installed_ids(recipient) == set()
    async with session_scope(tenant_id=str(owner_org), user_id=str(owner)) as session:
        assert await SkillInstallationsRepo(session).installed_ids(owner) == {str(skill)}
        assert (await session.execute(text("SELECT skill_id FROM skills WHERE skill_id=:id"),
                                      {"id": skill})).scalar_one() == skill
    with pytest.raises(DBAPIError):
        async with session_scope(tenant_id=str(owner_org), user_id=str(recipient)) as session:
            await SkillInstallationsRepo(session).install(owner, skill)


@pytest.mark.asyncio
async def test_company_exit_clears_installation_despite_retained_group_tuple(pg_engine):
    from vibecanvas_api.services.skill_installation_cleanup import discard_inaccessible_installations
    from unittest.mock import AsyncMock

    company, owner = await _seed_tenant_user_and_backfill(pg_engine)
    personal, recipient = await _seed_tenant_user_and_backfill(pg_engine)
    skill = uuid.uuid4()
    async with pg_engine.begin() as conn:
        await conn.execute(text("UPDATE organizations SET kind='business' WHERE tenant_id=:id"), {'id': company})
        await conn.execute(text("""INSERT INTO skills (skill_id, tenant_id, user_id, name, source)
            VALUES (:id,:org,:owner,'Company installation QA','custom')"""),
            {'id':skill,'org':company,'owner':owner})
        await conn.execute(text("""INSERT INTO org_memberships (membership_id,tenant_id,user_id,org_role,status)
            VALUES (:id,:org,:user,'member','active')"""),
            {'id':uuid.uuid4(),'org':company,'user':recipient})
    async with session_scope(tenant_id=str(personal), user_id=str(recipient)) as session:
        await SkillInstallationsRepo(session).install(recipient,skill)
    # A retained department tuple can still produce raw FGA allow after exit.
    client = AsyncMock()
    client.batch_check.return_value = (True,)
    assert await discard_inaccessible_installations(client, user_id=str(recipient)) == {(str(recipient),str(skill))}
    async with pg_engine.begin() as conn:
        await conn.execute(text("UPDATE org_memberships SET status='revoked' WHERE tenant_id=:org AND user_id=:user"),
                           {'org':company,'user':recipient})
    assert await discard_inaccessible_installations(client, user_id=str(recipient)) == set()
    async with pg_engine.begin() as conn:
        await conn.execute(text("UPDATE org_memberships SET status='active' WHERE tenant_id=:org AND user_id=:user"),
                           {'org':company,'user':recipient})
    async with session_scope(tenant_id=str(personal), user_id=str(recipient)) as session:
        assert await SkillInstallationsRepo(session).installed_ids(recipient) == set()


@pytest.mark.asyncio
async def test_workspace_mount_pruning_preserves_install_choices(pg_engine, monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from vibecanvas_api.authorization import openfga_client
    from vibecanvas_api.services.runtime_skills import prune_chat_skill_mounts

    personal, user = await _seed_tenant_user_and_backfill(pg_engine)
    company, owner = await _seed_tenant_user_and_backfill(pg_engine)
    personal_skill, company_skill = uuid.uuid4(), uuid.uuid4()
    async with pg_engine.begin() as conn:
        await conn.execute(text("UPDATE organizations SET kind='business' WHERE tenant_id=:id"), {'id': company})
        await conn.execute(text("""INSERT INTO org_memberships (membership_id,tenant_id,user_id,org_role,status)
            VALUES (:id,:org,:user,'member','active')"""),
            {'id':uuid.uuid4(),'org':company,'user':user})
        for skill, org, creator in [(personal_skill,personal,user),(company_skill,company,owner)]:
            await conn.execute(text("""INSERT INTO skills (skill_id,tenant_id,user_id,name,source)
                VALUES (:id,:org,:user,'Workspace package','custom')"""),
                {'id':skill,'org':org,'user':creator})
    async with session_scope(tenant_id=str(personal),user_id=str(user)) as session:
        for skill in [personal_skill,company_skill]:
            await SkillInstallationsRepo(session).install(user,skill)
    client=AsyncMock()
    client.batch_check.return_value=(True,True)
    monkeypatch.setattr(openfga_client,'openfga_client_from_config',lambda:client)
    for workspace,wanted,hidden in [(personal,personal_skill,company_skill),(company,company_skill,personal_skill)]:
        mount=tmp_path/str(workspace)/('a'*32)
        for skill in [personal_skill,company_skill]:
            (mount/str(skill)).mkdir(parents=True)
            (mount/str(skill)/'SKILL.md').write_text('Instruction')
        assert await prune_chat_skill_mounts(root=str(mount.parent),tenant_id=str(workspace),user_id=str(user)) == 1
        assert (mount/str(wanted)/'SKILL.md').exists()
        assert not (mount/str(hidden)).exists()
        async with session_scope(tenant_id=str(workspace),user_id=str(user)) as session:
            assert await SkillInstallationsRepo(session).installed_ids(user)=={str(personal_skill),str(company_skill)}
