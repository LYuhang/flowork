#!/usr/bin/env python3
"""Atomically cut an installation over to ciphertext-only content storage.

The command is safe to rerun. It pauses at each migration-only revision,
encrypts every pre-cutover Chat/Workflow/Task/Knowledge Base/Skill
file/private Template row, verifies that no plaintext broker payload
remains, and then applies all irreversible strict revisions through the current
head.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

# A migration workload may receive only the short-lived migration DSN.  Move
# it into the ordinary configuration slot before importing application modules
# so Alembic, backfill sessions and verification all use the same connection.
if migration_url := os.environ.get("MIGRATION_DATABASE_URL"):
    os.environ["DATABASE_URL"] = migration_url
    os.environ.pop("MAINTENANCE_DATABASE_URL", None)
    os.environ.pop("ADMIN_DATABASE_URL", None)

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text

from vibecanvas_api.security.content_backfill import (
    backfill_agent_run,
    backfill_account_deletion_emails,
    backfill_audit_private_payload,
    backfill_background_job,
    backfill_chat,
    backfill_hitl_chat,
    backfill_identity_user,
    backfill_knowledge_base,
    backfill_private_display_metadata,
    backfill_private_template,
    backfill_skill,
    backfill_task,
    backfill_vfs_abstracts,
    backfill_workflow,
    backfill_workflow_run,
)
from vibecanvas_api.security.migrate_legacy_secrets import (
    legacy_secret_count,
    migrate_legacy_deployment_hmacs,
    migrate_legacy_llm_connection_credentials,
    migrate_legacy_llm_secrets,
    migrate_legacy_mcp_bearers,
    migrate_legacy_mcp_connection_credentials,
    migrate_legacy_mcp_oauth_connections,
    migrate_legacy_mcp_oauth_transactions,
)
from vibecanvas_api.security.secret_service import suppress_secret_creation_audit
from vibecanvas_api.services.tenant_db import session_scope_admin
from vibecanvas_api.storage.db import dispose_engine, session_scope


REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve_api_root(repo_root: Path = REPO_ROOT) -> Path:
    """Locate Alembic assets in a source checkout or the API image layout."""
    for candidate in (repo_root / "api", repo_root):
        if (candidate / "alembic.ini").is_file() and (
            candidate / "alembic"
        ).is_dir():
            return candidate
    raise RuntimeError(
        "could not locate alembic.ini/alembic for strict content migration"
    )


API_ROOT = _resolve_api_root()


def _upgrade(target: str) -> None:
    cfg = AlembicConfig(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "alembic"))
    command.upgrade(cfg, target)


async def _migrate_secret_references() -> int:
    """Drain migration-only columns before revision 073 removes them."""
    async def has_column(table: str, column: str) -> bool:
        async with session_scope_admin() as session:
            return bool(
                (
                    await session.execute(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM "
                            "information_schema.columns WHERE "
                            "table_schema=current_schema() AND "
                            "table_name=:table AND column_name=:column)"
                        ),
                        {"table": table, "column": column},
                    )
                ).scalar_one()
            )

    migrated = 0
    migrations = [
        migrate_legacy_mcp_bearers,
        migrate_legacy_mcp_connection_credentials,
        migrate_legacy_llm_connection_credentials,
    ]
    if await has_column("llm_credentials", "api_key"):
        migrations.append(migrate_legacy_llm_secrets)
    if await has_column("mcp_oauth_connections", "access_token_encrypted"):
        migrations.append(migrate_legacy_mcp_oauth_connections)
    if await has_column("mcp_oauth_transactions", "code_verifier_encrypted"):
        migrations.append(migrate_legacy_mcp_oauth_transactions)
    if await has_column("deployments", "hmac_secret"):
        migrations.append(migrate_legacy_deployment_hmacs)
    for migrate in migrations:
        migrated += await migrate(batch_size=100)
    remaining = await legacy_secret_count()
    if remaining:
        raise RuntimeError(
            f"SecretService migration incomplete: {remaining} rows remain"
        )
    await dispose_engine()
    return migrated


async def _migrate_secret_references_without_audit() -> int:
    """Run the pre-097 backfill inside the Runner's own async context."""
    with suppress_secret_creation_audit():
        return await _migrate_secret_references()


async def _pending_resources(limit: int = 100) -> list[tuple[str, str, str]]:
    async with session_scope_admin() as session:
        rows = await session.execute(
            text(
                """
                SELECT kind, tenant_id::text, resource_id FROM (
                  SELECT 'chat' AS kind, c.tenant_id,
                         c.chat_id AS resource_id, min(m.id)::bigint AS ordering
                    FROM chats c
                    JOIN chat_messages m ON m.chat_id = c.chat_id
                   WHERE m.content_key_id IS NULL
                   GROUP BY c.tenant_id, c.chat_id
                  UNION ALL
                  SELECT 'workflow' AS kind, w.tenant_id,
                         w.wf_id AS resource_id, 0::bigint AS ordering
                    FROM workflows w
                    JOIN workflow_versions v ON v.wf_id = w.wf_id
                   WHERE v.workflow_key_id IS NULL
                   GROUP BY w.tenant_id, w.wf_id
                ) pending
                ORDER BY kind, ordering, resource_id
                LIMIT :limit
                """
            ),
            {"limit": max(1, min(limit, 1000))},
        )
        return [tuple(row) for row in rows.all()]


async def _migrate() -> tuple[int, int]:
    resources = rows = 0
    while True:
        pending = await _pending_resources()
        if not pending:
            break
        for kind, tenant_id, resource_id in pending:
            async with session_scope(tenant_id=tenant_id) as session:
                if kind == "chat":
                    rows += await backfill_chat(session, resource_id)
                else:
                    rows += await backfill_workflow(session, resource_id)
            resources += 1

    remaining = await _pending_resources(limit=1)
    if remaining:
        raise RuntimeError("content encryption migration did not converge")
    await dispose_engine()
    return resources, rows


async def _pending_tasks(limit: int = 100) -> list[tuple[str, str]]:
    async with session_scope_admin() as session:
        result = await session.execute(
            text(
                """
                SELECT DISTINCT t.tenant_id::text, t.id::text
                  FROM tasks t
                 WHERE t.content_key_id IS NULL
                    OR EXISTS (
                        SELECT 1 FROM task_events e
                         WHERE e.task_id=t.id AND e.payload_key_id IS NULL
                    )
                    OR EXISTS (
                        SELECT 1 FROM task_schedules s
                         WHERE s.task_id=t.id AND s.private_key_id IS NULL
                    )
                    OR EXISTS (
                        SELECT 1
                          FROM task_schedules s
                          JOIN scheduled_run_executions e
                            ON e.schedule_id=s.id
                         WHERE s.task_id=t.id AND e.private_key_id IS NULL
                    )
                 ORDER BY 2
                 LIMIT :limit
                """
            ),
            {"limit": max(1, min(limit, 1000))},
        )
        return [tuple(row) for row in result.all()]


async def _migrate_tasks() -> tuple[int, int]:
    resources = rows = 0
    while True:
        pending = await _pending_tasks()
        if not pending:
            break
        for tenant_id, task_id in pending:
            async with session_scope(tenant_id=tenant_id) as session:
                rows += await backfill_task(session, task_id)
            resources += 1
    if await _pending_tasks(limit=1):
        raise RuntimeError("Task content encryption migration did not converge")
    await dispose_engine()
    return resources, rows


async def _pending_knowledge_bases(limit: int = 100) -> list[tuple[str, str]]:
    async with session_scope_admin() as session:
        result = await session.execute(
            text(
                """
                SELECT DISTINCT kb.tenant_id::text, kb.id::text
                  FROM knowledge_bases kb
                 WHERE kb.private_key_id IS NULL
                    OR kb.name_lookup_hash IS NULL
                    OR EXISTS (
                        SELECT 1 FROM kb_files f
                         WHERE f.kb_id=kb.id AND f.private_key_id IS NULL
                    )
                    OR EXISTS (
                        SELECT 1 FROM kb_chunks c
                         WHERE c.kb_id=kb.id AND c.content_key_id IS NULL
                    )
                 ORDER BY 2
                 LIMIT :limit
                """
            ),
            {"limit": max(1, min(limit, 1000))},
        )
        return [tuple(row) for row in result.all()]


async def _migrate_knowledge_bases() -> tuple[int, int]:
    resources = rows = 0
    while True:
        pending = await _pending_knowledge_bases()
        if not pending:
            break
        for tenant_id, kb_id in pending:
            async with session_scope(tenant_id=tenant_id) as session:
                rows += await backfill_knowledge_base(session, kb_id)
            resources += 1
    if await _pending_knowledge_bases(limit=1):
        raise RuntimeError("Knowledge Base encryption migration did not converge")
    await dispose_engine()
    return resources, rows


async def _pending_agent_runs(limit: int = 100) -> list[tuple[str, str]]:
    async with session_scope_admin() as session:
        result = await session.execute(
            text(
                "SELECT DISTINCT r.tenant_id::text, r.run_id "
                "FROM agent_runs r WHERE r.private_key_id IS NULL OR EXISTS ("
                "SELECT 1 FROM agent_run_events e WHERE e.run_id=r.run_id "
                "AND e.payload_key_id IS NULL) ORDER BY 2 LIMIT :limit"
            ),
            {"limit": max(1, min(limit, 1000))},
        )
        return [tuple(row) for row in result.all()]


async def _migrate_agent_runs() -> tuple[int, int]:
    resources = rows = 0
    while True:
        pending = await _pending_agent_runs()
        if not pending:
            break
        for tenant_id, run_id in pending:
            async with session_scope(tenant_id=tenant_id) as session:
                rows += await backfill_agent_run(session, run_id)
            resources += 1
    if await _pending_agent_runs(limit=1):
        raise RuntimeError("Agent Run encryption migration did not converge")
    await dispose_engine()
    return resources, rows


async def _pending_hitl_chats(limit: int = 100) -> list[tuple[str, str]]:
    async with session_scope_admin() as session:
        result = await session.execute(
            text(
                "SELECT tenant_id::text, chat_id FROM ("
                "SELECT tenant_id, chat_id FROM hitl_requests "
                "WHERE private_key_id IS NULL UNION SELECT tenant_id, chat_id "
                "FROM interactive_artifacts WHERE private_key_id IS NULL"
                ") pending ORDER BY chat_id LIMIT :limit"
            ),
            {"limit": max(1, min(limit, 1000))},
        )
        return [tuple(row) for row in result.all()]


async def _migrate_hitl() -> tuple[int, int]:
    resources = rows = 0
    while True:
        pending = await _pending_hitl_chats()
        if not pending:
            break
        for tenant_id, chat_id in pending:
            async with session_scope(tenant_id=tenant_id) as session:
                rows += await backfill_hitl_chat(session, chat_id)
            resources += 1
    if await _pending_hitl_chats(limit=1):
        raise RuntimeError("HITL content encryption migration did not converge")
    await dispose_engine()
    return resources, rows


async def _pending_background_jobs(limit: int = 100) -> list[tuple[str, str]]:
    async with session_scope_admin() as session:
        result = await session.execute(
            text(
                "SELECT DISTINCT j.tenant_id::text, j.job_id "
                "FROM chat_tool_jobs j WHERE j.private_key_id IS NULL OR EXISTS ("
                "SELECT 1 FROM chat_tool_job_events e WHERE e.job_id=j.job_id "
                "AND e.payload_key_id IS NULL) ORDER BY 2 LIMIT :limit"
            ),
            {"limit": max(1, min(limit, 1000))},
        )
        return [tuple(row) for row in result.all()]


async def _migrate_background_jobs() -> tuple[int, int]:
    resources = rows = 0
    while True:
        pending = await _pending_background_jobs()
        if not pending:
            break
        for tenant_id, job_id in pending:
            async with session_scope(tenant_id=tenant_id) as session:
                rows += await backfill_background_job(session, job_id)
            resources += 1
    if await _pending_background_jobs(limit=1):
        raise RuntimeError("background job encryption migration did not converge")
    await dispose_engine()
    return resources, rows


async def _pending_execution_ledgers(
    limit: int = 100,
) -> list[tuple[str, str, str]]:
    async with session_scope_admin() as session:
        tables = (
            await session.execute(
                text(
                    "SELECT to_regclass('workflow_run_state') IS NOT NULL, "
                    "to_regclass('workflow_run_events') IS NOT NULL"
                )
            )
        ).one()
        queries: list[str] = []
        if tables[0] and tables[1]:
            queries.append(
                "SELECT 'workflow_run' AS kind, s.tenant_id, "
                "s.wf_id AS resource_id FROM workflow_run_state s "
                "WHERE s.private_key_id IS NULL OR EXISTS ("
                "SELECT 1 FROM workflow_run_events e WHERE e.wf_id=s.wf_id "
                "AND e.payload_key_id IS NULL)"
            )
        if not queries:
            return []
        result = await session.execute(
            text(
                "SELECT kind, tenant_id::text, resource_id FROM ("
                + " UNION ALL ".join(queries)
                + ") pending "
                "ORDER BY kind, resource_id LIMIT :limit"
            ),
            {"limit": max(1, min(limit, 1000))},
        )
        return [tuple(row) for row in result.all()]


async def _migrate_execution_ledgers() -> tuple[int, int]:
    resources = rows = 0
    while True:
        pending = await _pending_execution_ledgers()
        if not pending:
            break
        for kind, tenant_id, resource_id in pending:
            async with session_scope(tenant_id=tenant_id) as session:
                rows += await backfill_workflow_run(session, resource_id)
            resources += 1
    if await _pending_execution_ledgers(limit=1):
        raise RuntimeError("execution ledger encryption migration did not converge")
    await dispose_engine()
    return resources, rows


async def _pending_private_display_metadata(
    limit: int = 100,
) -> list[tuple[str, str, str]]:
    async with session_scope_admin() as session:
        result = await session.execute(
            text(
                "SELECT kind, tenant_id::text, resource_id FROM ("
                "SELECT 'chat' AS kind, tenant_id, chat_id AS resource_id "
                "FROM chats WHERE metadata_key_id IS NULL UNION ALL "
                "SELECT 'workflow' AS kind, tenant_id, wf_id AS resource_id "
                "FROM workflows w WHERE metadata_key_id IS NULL OR EXISTS ("
                "SELECT 1 FROM workflow_versions v WHERE v.wf_id=w.wf_id "
                "AND v.note_key_id IS NULL) UNION ALL "
                "SELECT 'task_schedule' AS kind, tenant_id, id::text AS resource_id "
                "FROM task_schedules WHERE private_schema_version IS DISTINCT FROM 2"
                ") pending ORDER BY kind, resource_id LIMIT :limit"
            ),
            {"limit": max(1, min(limit, 1000))},
        )
        return [tuple(row) for row in result.all()]


async def _migrate_private_display_metadata() -> tuple[int, int]:
    resources = rows = 0
    while True:
        pending = await _pending_private_display_metadata()
        if not pending:
            break
        for kind, tenant_id, resource_id in pending:
            async with session_scope(tenant_id=tenant_id) as session:
                rows += await backfill_private_display_metadata(
                    session,
                    kind,
                    resource_id,
                )
            resources += 1
    if await _pending_private_display_metadata(limit=1):
        raise RuntimeError("private display metadata migration did not converge")
    await dispose_engine()
    return resources, rows


async def _pending_skills(limit: int = 100) -> list[tuple[str, str]]:
    async with session_scope_admin() as session:
        result = await session.execute(
            text(
                "SELECT DISTINCT s.tenant_id::text, s.skill_id::text "
                "FROM skills s WHERE EXISTS (SELECT 1 "
                "FROM skill_revision_files f WHERE f.skill_id=s.skill_id "
                "AND f.content_key_id IS NULL) OR EXISTS (SELECT 1 "
                "FROM skill_draft_files f WHERE f.skill_id=s.skill_id "
                "AND f.content_key_id IS NULL) ORDER BY 2 LIMIT :limit"
            ),
            {"limit": max(1, min(limit, 1000))},
        )
        return [tuple(row) for row in result.all()]


async def _migrate_skills() -> tuple[int, int]:
    resources = rows = 0
    while True:
        pending = await _pending_skills()
        if not pending:
            break
        for tenant_id, skill_id in pending:
            async with session_scope(tenant_id=tenant_id) as session:
                rows += await backfill_skill(session, skill_id)
            resources += 1
    if await _pending_skills(limit=1):
        raise RuntimeError("Skill file encryption migration did not converge")
    await dispose_engine()
    return resources, rows


async def _pending_private_templates(limit: int = 100) -> list[tuple[str, str]]:
    async with session_scope_admin() as session:
        result = await session.execute(
            text(
                "SELECT tenant_id::text, template_id FROM templates "
                "WHERE visibility='private' AND private_key_id IS NULL "
                "ORDER BY template_id LIMIT :limit"
            ),
            {"limit": max(1, min(limit, 1000))},
        )
        return [tuple(row) for row in result.all()]


async def _migrate_private_templates() -> tuple[int, int]:
    resources = rows = 0
    while True:
        pending = await _pending_private_templates()
        if not pending:
            break
        for tenant_id, template_id in pending:
            async with session_scope(tenant_id=tenant_id) as session:
                rows += await backfill_private_template(session, template_id)
            resources += 1
    if await _pending_private_templates(limit=1):
        raise RuntimeError("private Template migration did not converge")
    await dispose_engine()
    return resources, rows


async def _pending_identity_users(limit: int = 100) -> list[tuple[str, str]]:
    async with session_scope_admin() as session:
        result = await session.execute(
            text(
                "SELECT u.tenant_id::text, u.user_id::text FROM users u "
                "WHERE u.profile_key_id IS NULL OR EXISTS (SELECT 1 "
                "FROM auth_identities i WHERE i.user_id=u.user_id AND "
                "i.provider_uid_key_id IS NULL) ORDER BY u.user_id LIMIT :limit"
            ),
            {"limit": max(1, min(limit, 1000))},
        )
        return [tuple(row) for row in result.all()]


async def _migrate_identity_users() -> tuple[int, int, int]:
    # Very old installations can contain identity rows whose user was removed
    # before the ON DELETE CASCADE constraint existed. They have no tenant
    # from which to derive an encryption context and cannot authenticate any
    # account. Purge only those unreachable PII remnants; never delete or
    # synthesize an active user/identity to make the strict gate pass.
    async with session_scope_admin() as session:
        orphan_result = await session.execute(
            text(
                "DELETE FROM auth_identities AS identity "
                "WHERE NOT EXISTS (SELECT 1 FROM users AS user_row "
                "WHERE user_row.user_id=identity.user_id)"
            )
        )
        orphan_rows = orphan_result.rowcount

    resources = rows = 0
    while True:
        pending = await _pending_identity_users()
        if not pending:
            break
        for tenant_id, user_id in pending:
            async with session_scope(tenant_id=tenant_id) as session:
                rows += await backfill_identity_user(session, user_id)
            resources += 1
    if await _pending_identity_users(limit=1):
        raise RuntimeError("identity PII migration did not converge")
    await dispose_engine()
    return resources, rows, max(0, orphan_rows or 0)


async def _pending_deletion_users(limit: int = 100) -> list[tuple[str, str]]:
    async with session_scope_admin() as session:
        result = await session.execute(
            text(
                "SELECT DISTINCT u.tenant_id::text, u.user_id::text "
                "FROM users u JOIN account_deletion_requests d "
                "ON d.user_id=u.user_id WHERE d.email_snapshot_key_id IS NULL "
                "ORDER BY 2 LIMIT :limit"
            ),
            {"limit": max(1, min(limit, 1000))},
        )
        return [tuple(row) for row in result.all()]


async def _migrate_deletion_emails() -> tuple[int, int]:
    resources = rows = 0
    while True:
        pending = await _pending_deletion_users()
        if not pending:
            break
        for tenant_id, user_id in pending:
            async with session_scope(tenant_id=tenant_id) as session:
                rows += await backfill_account_deletion_emails(session, user_id)
            resources += 1
    if await _pending_deletion_users(limit=1):
        raise RuntimeError("account deletion PII migration did not converge")
    await dispose_engine()
    return resources, rows


async def _pending_audit_rows(limit: int = 200) -> list[str]:
    async with session_scope_admin() as session:
        result = await session.execute(
            text(
                "SELECT audit_id::text FROM audit_log "
                "WHERE actor_email IS NOT NULL OR target_name IS NOT NULL "
                "OR ip_address IS NOT NULL OR user_agent IS NOT NULL "
                "OR meta<>'{}'::jsonb OR (tenant_id IS NOT NULL "
                "AND private_key_id IS NULL) "
                "ORDER BY created_at, audit_id LIMIT :limit"
            ),
            {"limit": max(1, min(limit, 2000))},
        )
        return list(result.scalars())


async def _migrate_audit_rows() -> int:
    # Revision 097 grants this on new upgrades. Reassert it here as well so a
    # deployment that had already paused on 097 before that migration fix can
    # resume safely. Revision 098 revokes the temporary write privilege again.
    async with session_scope_admin() as session:
        await session.execute(
            text(
                "GRANT SELECT, UPDATE ON audit_log TO vibecanvas_migrator"
            )
        )
    rows = 0
    while True:
        pending = await _pending_audit_rows()
        if not pending:
            break
        async with session_scope_admin() as session:
            for audit_id in pending:
                rows += await backfill_audit_private_payload(session, audit_id)
    if await _pending_audit_rows(limit=1):
        raise RuntimeError("audit private migration did not converge")
    await dispose_engine()
    return rows


async def _pending_vfs_abstract_resources(
    limit: int = 100,
) -> list[tuple[str, str, str]]:
    async with session_scope_admin() as session:
        result = await session.execute(
            text(
                "SELECT kind, tenant_id::text, resource_id FROM ("
                "SELECT DISTINCT 'artifact' AS kind, tenant_id, "
                "scope_id AS resource_id FROM vfs_artifacts WHERE abstract<>'' "
                "UNION ALL SELECT DISTINCT 'scratch' AS kind, tenant_id, "
                "scope_id AS resource_id FROM vfs_scratch WHERE abstract<>'' "
                "UNION ALL SELECT DISTINCT 'run' AS kind, tenant_id, "
                "run_id AS resource_id FROM vfs_run WHERE abstract<>''"
                ") pending ORDER BY kind, resource_id LIMIT :limit"
            ),
            {"limit": max(1, min(limit, 1000))},
        )
        return [tuple(row) for row in result.all()]


async def _migrate_vfs_abstracts() -> tuple[int, int]:
    resources = rows = 0
    while True:
        pending = await _pending_vfs_abstract_resources()
        if not pending:
            break
        for kind, tenant_id, resource_id in pending:
            async with session_scope(tenant_id=tenant_id) as session:
                rows += await backfill_vfs_abstracts(
                    session,
                    kind=kind,
                    resource_id=resource_id,
                )
            resources += 1
    if await _pending_vfs_abstract_resources(limit=1):
        raise RuntimeError("VFS abstract encryption migration did not converge")
    await dispose_engine()
    return resources, rows


def main() -> None:
    # Each irreversible cutover is preceded by a migration-only revision that
    # exposes legacy plaintext solely to this deployment command. Keep one
    # Runner alive across every backfill phase: SQLAlchemy/asyncpg pools are
    # event-loop bound, while repeated ``asyncio.run`` calls create a new loop
    # and can strand a connection from an earlier phase.
    with asyncio.Runner() as runner:
        _upgrade("067")
        # Secret rows are created before revision 097 introduces the current
        # encrypted audit shape. This deployment-only backfill is summarized
        # once by the command output instead of emitting one future-schema ORM
        # audit row per migrated value. Runtime SecretService writes continue
        # to audit by default.
        secret_rows = runner.run(_migrate_secret_references_without_audit())
        resources, rows = runner.run(_migrate())
        _upgrade("069")
        task_resources, task_rows = runner.run(_migrate_tasks())
        _upgrade("071")
        kb_resources, kb_rows = runner.run(_migrate_knowledge_bases())
        _upgrade("076")
        run_resources, run_rows = runner.run(_migrate_agent_runs())
        _upgrade("078")
        hitl_resources, hitl_rows = runner.run(_migrate_hitl())
        _upgrade("080")
        background_resources, background_rows = runner.run(
            _migrate_background_jobs()
        )
        _upgrade("082")
        execution_resources, execution_rows = runner.run(
            _migrate_execution_ledgers()
        )
        _upgrade("084")
        metadata_resources, metadata_rows = runner.run(
            _migrate_private_display_metadata()
        )
        _upgrade("089")
        skill_resources, skill_rows = runner.run(_migrate_skills())
        _upgrade("091")
        template_resources, template_rows = runner.run(
            _migrate_private_templates()
        )
        _upgrade("093")
        identity_resources, identity_rows, orphan_identity_rows = runner.run(
            _migrate_identity_users()
        )
        _upgrade("095")
        deletion_resources, deletion_rows = runner.run(
            _migrate_deletion_emails()
        )
        _upgrade("097")
        audit_rows = runner.run(_migrate_audit_rows())
        _upgrade("099")
        vfs_resources, vfs_rows = runner.run(_migrate_vfs_abstracts())
        _upgrade("head")
        runner.run(dispose_engine())
    print(
        "strict content encryption complete: "
        f"{rows + task_rows + kb_rows + run_rows + hitl_rows + background_rows + execution_rows + metadata_rows + skill_rows + template_rows + identity_rows + deletion_rows + audit_rows + vfs_rows} "
        "content rows and "
        f"{secret_rows} secret rows; "
        f"{orphan_identity_rows} unreachable orphan identity rows purged; "
        "content encrypted across "
        f"{resources + task_resources + kb_resources + run_resources + hitl_resources + background_resources + execution_resources + metadata_resources + skill_resources + template_resources + identity_resources + deletion_resources + vfs_resources} "
        "Chat/Workflow/Task/Knowledge Base/Agent Run/HITL/background/"
        "execution/metadata/Skill/Template/identity/deletion/VFS resources"
    )


if __name__ == "__main__":
    main()
