"""Background task wrapping :class:`KbIndexer`.

Sync task body. All DB writes go through ``run_in_short_session(lambda
s: ...)`` because the indexer service is async — the lambda body IS
async (it receives an :class:`AsyncSession`) and that's fine because
``run_in_short_session`` does ``asyncio.run(coro)`` internally; the
task itself stays sync.

Status state machine: ``pending → indexing → indexed | failed``.

Failure contract:

* ``IndexingError`` (terminal indexing failure — bad doc, too many
  chunks, etc.) — clean any partial chunks and set ``kb_files.status
  = "failed"`` with ``error_message``. Do not re-raise; DBOS recovery is
  reserved for process interruption, not indexing failures the caller can fix.
* Unknown :class:`Exception` — same cleanup path, generic error
  message, same no-re-raise contract.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from vibecanvas_api.authorization.dependencies import (
    authz_service_for_session,
)
from vibecanvas_api.authorization.openfga_client import (
    openfga_client_from_config,
)
from vibecanvas_api.authorization.service import AuthorizationDeniedError
from vibecanvas_api.authorization.types import (
    Action,
    AuthzRequestContext,
    Decision,
    PrincipalRef,
    PrincipalType,
    ResourceRef,
    ResourceType,
)
from vibecanvas_api.services.kb_indexer import IndexingError, KbIndexer
from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.storage.models_kb import KbFile
from vibecanvas_api.storage.models_org import OrgMembership
from vibecanvas_api.storage.repo_kb import KbRepo
from vibecanvas_api.storage.sync_session import (
    current_sync_tenant_id,
    run_in_short_session,
)


async def _require_captured_user_update(
    session,
    *,
    tenant_id: str,
    file_id: uuid.UUID,
    user_id: str | None,
) -> None:
    """Re-authorize the captured user before the worker mutates KB state."""
    file_row = (
        await session.execute(
            select(KbFile.kb_id, KbFile.user_id).where(
                KbFile.id == file_id,
                KbFile.deleted_at.is_(None),
            )
        )
    ).one_or_none()
    if file_row is None:
        raise AuthorizationDeniedError(
            decision=Decision(False, reason_code="resource_not_found"),
        )
    captured_user_id = user_id or str(file_row.user_id)
    membership = (
        await session.execute(
            select(OrgMembership).where(
                OrgMembership.tenant_id == uuid.UUID(tenant_id),
                OrgMembership.user_id == uuid.UUID(captured_user_id),
            )
        )
    ).scalar_one_or_none()
    if membership is None or membership.status != "active":
        raise AuthorizationDeniedError(
            decision=Decision(
                False,
                reason_code="inactive_organization_membership",
            ),
        )
    membership_role = membership.org_role
    membership_status = membership.status
    client = openfga_client_from_config()
    try:
        service = authz_service_for_session(
            session=session,
            organization_id=tenant_id,
            openfga_client=client,
        )
        await service.require(
            PrincipalRef(PrincipalType.USER, captured_user_id),
            Action.UPDATE,
            ResourceRef(
                ResourceType.KNOWLEDGE_BASE_FILE,
                str(file_id),
                tenant_id,
            ),
            AuthzRequestContext(
                active_organization_id=tenant_id,
                membership_role=membership_role,
                membership_status=membership_status,
            ),
        )
    finally:
        if client is not None:
            await client.close()

def kb_index_file_task(
    task_id: str,
    tenant_id: str,
    file_id: str,
    user_id: str | None = None,
):
    """Sync body. Mirrors ``batch_exec.py:179`` pattern — set
    ``current_sync_tenant_id`` ONCE at the top (str, not UUID), then
    every ``run_in_short_session`` reads that CV and auto-runs
    ``SELECT set_config('app.tenant_id', :t, true)`` so RLS is honoured
    for every short-session write.
    """
    # The tenant ContextVar is a string, matching batch execution.
    current_sync_tenant_id.set(tenant_id)
    fid = uuid.UUID(file_id)

    try:
        run_in_short_session(
            lambda s: _require_captured_user_update(
                s,
                tenant_id=tenant_id,
                file_id=fid,
                user_id=user_id,
            )
        )
    except AuthorizationDeniedError:
        # Marking the platform-owned job outcome does not grant the captured
        # user content access; it prevents an orphan reconciler from repeatedly
        # re-enqueuing work after access was revoked.
        run_in_short_session(
            lambda s: KbRepo(s).set_file_status(
                fid,
                status="failed",
                error_message="Authorization no longer permits indexing.",
            )
        )
        return

    # pending → indexing (kb_files). KB indexing is tracked by the KB file row,
    # not by the platform Task center.
    run_in_short_session(
        lambda s: KbRepo(s).set_file_status(fid, status="indexing"))

    try:
        # The indexer service is async — wrap it in a coro-factory that
        # ``run_in_short_session`` will dispatch via asyncio.run.
        def _run(s):
            indexer = KbIndexer(s, get_object_store())
            return indexer.index_file(fid)

        chunk_count = run_in_short_session(_run)

        # indexing → indexed
        run_in_short_session(
            lambda s: KbRepo(s).set_file_status(
                fid, status="indexed", chunk_count=chunk_count))

    except IndexingError as exc:
        # Clean partial derived rows so a later package update cannot see
        # stale search data; then retain the original file and mark only the
        # disposable derivation as failed.
        error_message = str(exc)
        run_in_short_session(
            lambda s: KbRepo(s).delete_chunks_for_file(fid))
        run_in_short_session(
            lambda s: KbRepo(s).set_file_status(
                fid, status="failed", error_message=error_message))
    except Exception as exc:
        # Unknown failure — same cleanup, generic error_message.
        # Do NOT re-raise: keep DBOS recovery reserved for whole-task
        # interruption (worker termination/OOM) that never reaches this
        # except block at all.
        error_message = (
            f"unexpected: {type(exc).__name__}: {exc}"
        )
        run_in_short_session(
            lambda s: KbRepo(s).delete_chunks_for_file(fid))
        run_in_short_session(
            lambda s: KbRepo(s).set_file_status(
                fid, status="failed",
                error_message=error_message))
