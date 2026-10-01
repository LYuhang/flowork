"""Encrypted, append-only execution evidence and durable approval commands.

All mutations lock the execution row first. Runtime event sequence and process
generation fence retries; a commit must precede the runtime acknowledgement.
No method reconstructs or resumes a lost execution from stored history.
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.security.content_encryption import content_encryption_service

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timed_out", "cancelled"})
RUN_STATUSES = TERMINAL_STATUSES | {"queued", "running", "waiting_approval"}
PENDING_APPROVALS = ("pending", "decision_requested")


class HistoryConflict(ValueError):
    """A stale generation, out-of-order event or closed execution."""


def _public(row) -> dict:
    return {
        k: (v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, uuid.UUID) else v)
        for k, v in row.items()
        if not k.endswith(("_ciphertext", "_nonce", "_key_id")) and k != "runtime_process"
    }


class WorkflowHistoryRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _encrypt(self, run, purpose: str, record_id: str, value):
        return await content_encryption_service().encrypt_json(
            self.session,
            tenant_id=run["tenant_id"],
            resource_type="workflow_execution_history",
            resource_id=str(run["id"]),
            purpose=purpose,
            record_id=record_id,
            value=value,
        )

    async def _decrypt(self, run, row, prefix: str, purpose: str, record_id: str):
        if row[f"{prefix}_key_id"] is None:
            return None
        return await content_encryption_service().decrypt_json(
            self.session,
            tenant_id=run["tenant_id"],
            resource_type="workflow_execution_history",
            resource_id=str(run["id"]),
            purpose=purpose,
            record_id=record_id,
            key_id=row[f"{prefix}_key_id"],
            ciphertext=row[f"{prefix}_ciphertext"],
            nonce=row[f"{prefix}_nonce"],
        )

    async def get(self, execution_id: str, *, lock: bool = False):
        suffix = " FOR UPDATE" if lock else ""
        return (
            (
                await self.session.execute(
                    text(
                        "SELECT * FROM workflow_execution_runs WHERE id=:id" + suffix,
                    ),
                    {"id": uuid.UUID(execution_id)},
                )
            )
            .mappings()
            .one_or_none()
        )

    async def create(
        self,
        *,
        execution_id: str,
        tenant_id: str,
        wf_id: str,
        source_type: str,
        source_id: str,
        initiator_user_id: str | None,
        workflow: dict,
        inputs: dict,
        approvers: dict[str, str],
        revision_id: str | None = None,
        node_id: str | None = None,
        input_index: int | None = None,
    ) -> None:
        run = {"id": uuid.UUID(execution_id), "tenant_id": uuid.UUID(tenant_id)}
        private = await self._encrypt(
            run,
            "execution_private",
            execution_id,
            {"workflow": workflow, "inputs": inputs, "approvers": approvers, "node_id": node_id},
        )
        await self.session.execute(
            text("""INSERT INTO workflow_execution_runs
            (id,tenant_id,wf_id,source_type,source_id,initiator_user_id,revision_id,input_index,
             private_ciphertext,private_nonce,private_key_id)
            VALUES (:id,:tenant_id,:wf_id,:source_type,:source_id,:initiator,:revision,:input_index,
                    :ciphertext,:nonce,:key_id)"""),
            {
                **run,
                "wf_id": wf_id,
                "source_type": source_type,
                "source_id": source_id,
                "initiator": uuid.UUID(initiator_user_id) if initiator_user_id else None,
                "revision": revision_id,
                "input_index": input_index,
                "ciphertext": private.ciphertext,
                "nonce": private.nonce,
                "key_id": private.key_id,
            },
        )

    async def claim_dispatch(self, execution_id: str) -> None:
        claimed = await self.session.scalar(
            text("""UPDATE workflow_execution_runs SET dispatch_claim=:claim
            WHERE id=:id AND status='queued' AND generation IS NULL AND dispatch_claim IS NULL RETURNING id"""),
            {"id": uuid.UUID(execution_id), "claim": uuid.uuid4()},
        )
        if claimed is None:
            raise HistoryConflict("execution_already_dispatched")

    async def bind_runtime(self, execution_id: str, generation: str, *, process: dict | None = None) -> None:
        run = await self.get(execution_id, lock=True)
        if run is None:
            raise KeyError(execution_id)
        if run["generation"] not in (None, generation) or run["status"] in TERMINAL_STATUSES:
            raise HistoryConflict("execution_runtime_changed")
        await self.session.execute(
            text("""UPDATE workflow_execution_runs
            SET generation=:generation, status='running', started_at=COALESCE(started_at,now()),
                runtime_process=CAST(:process AS jsonb)
            WHERE id=:id AND generation IS NULL"""),
            {"id": run["id"], "generation": generation, "process": json.dumps(process) if process else None},
        )

    async def persist_events(self, execution_id: str, generation: str, events: list[dict]) -> int:
        """Store an ordered batch atomically; return the watermark to ACK after commit."""
        run = await self.get(execution_id, lock=True)
        if run is None:
            raise KeyError(execution_id)
        if run["generation"] != generation:
            raise HistoryConflict("execution_runtime_changed")
        last_seq = run["last_seq"]
        status = run["status"]
        private = None
        for event in events:
            if event.get("invocation_id") != execution_id or event.get("generation") != generation:
                raise HistoryConflict("execution_event_identity_mismatch")
            seq = event["seq"]
            if type(seq) is not int or seq <= 0:
                raise HistoryConflict("invalid_execution_event_sequence")
            if seq <= last_seq:
                continue
            if seq != last_seq + 1 or status in TERMINAL_STATUSES:
                raise HistoryConflict("execution_event_out_of_order")
            kind = event["type"]
            encrypted = await self._encrypt(run, "execution_event", f"{execution_id}:{seq}", event)
            await self.session.execute(
                text("""INSERT INTO workflow_execution_events
                (tenant_id,execution_id,seq,event_type,payload_ciphertext,payload_nonce,payload_key_id)
                VALUES (:tenant,:id,:seq,:kind,:ciphertext,:nonce,:key_id)"""),
                {
                    "tenant": run["tenant_id"],
                    "id": run["id"],
                    "seq": seq,
                    "kind": kind,
                    "ciphertext": encrypted.ciphertext,
                    "nonce": encrypted.nonce,
                    "key_id": encrypted.key_id,
                },
            )
            if kind == "approval_requested":
                if private is None:
                    private = await self._decrypt(run, run, "private", "execution_private", execution_id)
                approver = private["approvers"].get(event["node_id"])
                if approver is None:
                    raise HistoryConflict("execution_approval_assignee_missing")
                await self.session.execute(
                    text("""INSERT INTO workflow_execution_approvals
                    (id,tenant_id,execution_id,node_id,approver_user_id,deadline)
                    VALUES (:approval,:tenant,:id,:node,:approver,:deadline)"""),
                    {
                        "approval": event["approval_id"],
                        "tenant": run["tenant_id"],
                        "id": run["id"],
                        "node": event["node_id"],
                        "approver": uuid.UUID(approver),
                        "deadline": datetime.fromtimestamp(event["deadline"], tz=timezone.utc),
                    },
                )
                status = "waiting_approval"
            elif kind == "approval_ready":
                # The decision is durable, but execution remains blocked until
                # the host reauthorizes its original principal and resumes it.
                if (
                    event.get("reason") not in {"approved", "rejected", "timeout"}
                    or type(event.get("approved")) is not bool
                    or event["approved"] != (event["reason"] == "approved")
                ):
                    raise HistoryConflict("invalid_approval_resolution")
                pending = await self.session.scalar(
                    text("""SELECT EXISTS (
                    SELECT 1 FROM workflow_execution_approvals WHERE id=:approval AND execution_id=:id
                    AND status IN ('pending','decision_requested'))"""),
                    {"approval": event["approval_id"], "id": run["id"]},
                )
                if not pending:
                    raise HistoryConflict("unknown_or_closed_approval")
                status = "waiting_approval"
            elif kind == "approval_resolved":
                reason = event["reason"]
                approved = event["approved"]
                if reason not in {"approved", "rejected", "timeout"} or type(approved) is not bool:
                    raise HistoryConflict("invalid_approval_resolution")
                if approved != (reason == "approved"):
                    raise HistoryConflict("invalid_approval_resolution")
                resolved = (
                    await self.session.execute(
                        text("""UPDATE workflow_execution_approvals
                    SET status=:reason, approved=:approved, resolved_at=:resolved
                    WHERE id=:approval AND execution_id=:id AND status IN ('pending','decision_requested')
                    RETURNING id"""),
                        {
                            "reason": reason,
                            "approved": approved,
                            "approval": event["approval_id"],
                            "id": run["id"],
                            "resolved": datetime.fromtimestamp(event["decided_at"], tz=timezone.utc),
                        },
                    )
                ).scalar_one_or_none()
                if resolved is None:
                    raise HistoryConflict("unknown_or_closed_approval")
                pending = await self.session.scalar(
                    text("""SELECT EXISTS (SELECT 1
                    FROM workflow_execution_approvals WHERE execution_id=:id
                    AND status IN ('pending','decision_requested'))"""),
                    {"id": run["id"]},
                )
                status = "waiting_approval" if pending else "running"
            elif kind == "result":
                status = event["status"]
                if status not in TERMINAL_STATUSES:
                    raise HistoryConflict("invalid_execution_terminal_status")
                result = {k: event.get(k) for k in ("final_outputs", "error_dict", "execution_time")}
                await self._finish(
                    run, status=status, result=result, error_code="execution_failed" if status == "failed" else None
                )
            elif kind != "node_event":
                raise HistoryConflict("unknown_execution_event")
            last_seq = seq
        await self.session.execute(
            text("""UPDATE workflow_execution_runs
            SET last_seq=:seq,status=:status WHERE id=:id"""),
            {"id": run["id"], "seq": last_seq, "status": status},
        )
        return last_seq

    async def _finish(self, run, *, status: str, result: dict, error_code: str | None):
        encrypted = await self._encrypt(run, "execution_result", str(run["id"]), result)
        await self.session.execute(
            text("""UPDATE workflow_execution_runs SET status=:status,
            finished_at=now(),error_code=:error,result_ciphertext=:ciphertext,
            result_nonce=:nonce,result_key_id=:key_id WHERE id=:id"""),
            {
                "id": run["id"],
                "status": status,
                "error": error_code,
                "ciphertext": encrypted.ciphertext,
                "nonce": encrypted.nonce,
                "key_id": encrypted.key_id,
            },
        )
        await self.session.execute(
            text("""UPDATE workflow_execution_approvals
            SET status=:reason,resolved_at=now() WHERE execution_id=:id
            AND status IN ('pending','decision_requested')"""),
            {"id": run["id"], "reason": "execution_lost" if error_code == "execution_lost" else "cancelled"},
        )

    async def confirm_cancelled(self, execution_id: str):
        """Only the process owner calls this after confirming execution stopped."""
        run = await self.get(execution_id, lock=True)
        if run is None:
            raise KeyError(execution_id)
        if run["status"] in TERMINAL_STATUSES:
            return
        if run["cancel_requested_at"] is None:
            raise HistoryConflict("cancellation_not_requested")
        await self._finish(
            run,
            status="cancelled",
            error_code=None,
            result={
                "final_outputs": {},
                "error_dict": {"__engine__": "execution_cancelled"},
                "execution_time": None,
            },
        )

    async def fail(self, execution_id: str, *, error_code: str, generation: str | None = None):
        """Called only after confirmed execution loss or a pre-dispatch failure."""
        if error_code not in {
            "execution_lost",
            "execution_dispatch_failed",
            "execution_failed",
            "execution_unavailable",
            "execution_quota_exceeded",
            "execution_resume_failed",
        }:
            raise ValueError("unsupported execution failure code")
        run = await self.get(execution_id, lock=True)
        if run is None:
            raise KeyError(execution_id)
        if generation is not None and run["generation"] != generation:
            raise HistoryConflict("execution_runtime_changed")
        if run["status"] in TERMINAL_STATUSES:
            return
        await self._finish(
            run,
            status="failed",
            error_code=error_code,
            result={"final_outputs": {}, "error_dict": {"__engine__": error_code}, "execution_time": None},
        )

    async def request_decision(
        self, execution_id: str, approval_id: str, *, actor_user_id: str, approved: bool
    ) -> dict:
        if type(approved) is not bool:
            raise ValueError("approved must be a boolean")
        run = await self.get(execution_id, lock=True)
        if run is None:
            raise KeyError(execution_id)
        row = (
            (
                await self.session.execute(
                    text("""SELECT * FROM workflow_execution_approvals
            WHERE id=:approval AND execution_id=:id AND approver_user_id=:actor"""),
                    {"approval": approval_id, "id": run["id"], "actor": uuid.UUID(actor_user_id)},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise KeyError(approval_id)
        if run["status"] in TERMINAL_STATUSES or row["status"] not in PENDING_APPROVALS:
            raise HistoryConflict("approval_no_longer_pending")
        # Database clock avoids accepting late decisions if a host clock drifts.
        on_time = await self.session.scalar(text("SELECT clock_timestamp() < :deadline"), {"deadline": row["deadline"]})
        if not on_time:
            raise HistoryConflict("approval_deadline_passed")
        if row["status"] == "decision_requested":
            if row["requested_decision"] != approved:
                raise HistoryConflict("approval_decision_already_requested")
            return _public(row)
        await self.session.execute(
            text("""UPDATE workflow_execution_approvals
            SET status='decision_requested',requested_decision=:approved,requested_by=:actor,
                decision_requested_at=clock_timestamp() WHERE id=:approval"""),
            {"approval": approval_id, "approved": approved, "actor": uuid.UUID(actor_user_id)},
        )
        return {
            **_public(row),
            "status": "decision_requested",
            "requested_decision": approved,
            "requested_by": actor_user_id,
        }

    async def pending_commands(self, execution_id: str) -> list[dict]:
        rows = (
            await self.session.execute(
                text("""SELECT a.* FROM workflow_execution_approvals a
            JOIN workflow_execution_runs r ON r.id=a.execution_id
            WHERE a.execution_id=:id AND a.status='decision_requested'
            AND r.status IN ('running','waiting_approval') ORDER BY a.decision_requested_at,a.id"""),
                {"id": uuid.UUID(execution_id)},
            )
        ).mappings()
        return [_public(row) for row in rows]

    async def request_cancel(self, execution_id: str) -> dict:
        run = await self.get(execution_id, lock=True)
        if run is None:
            raise KeyError(execution_id)
        if run["status"] in TERMINAL_STATUSES:
            raise HistoryConflict("execution_already_finished")
        await self.session.execute(
            text("""UPDATE workflow_execution_runs
            SET cancel_requested_at=COALESCE(cancel_requested_at,now()) WHERE id=:id"""),
            {"id": run["id"]},
        )
        return {"execution_id": execution_id, "status": run["status"], "cancel_requested": True}

    async def is_assignee(self, execution_id: str, user_id: str) -> bool:
        return bool(
            await self.session.scalar(
                text("""SELECT EXISTS (SELECT 1
            FROM workflow_execution_approvals WHERE execution_id=:id AND approver_user_id=:actor)"""),
                {"id": uuid.UUID(execution_id), "actor": uuid.UUID(user_id)},
            )
        )

    async def result_detail(self, execution_id: str) -> dict | None:
        """Read final evidence without decrypting the graph, inputs or node logs."""
        run = await self.get(execution_id)
        if run is None:
            return None
        return {
            **_public(run),
            "result": await self._decrypt(run, run, "result", "execution_result", execution_id),
        }

    async def detail(self, execution_id: str) -> dict | None:
        run = await self.get(execution_id)
        if run is None:
            return None
        private = await self._decrypt(run, run, "private", "execution_private", execution_id)
        approvals = (
            await self.session.execute(
                text("""SELECT * FROM workflow_execution_approvals
            WHERE execution_id=:id ORDER BY requested_at,id"""),
                {"id": run["id"]},
            )
        ).mappings()
        return {
            **_public(run),
            "workflow": private["workflow"],
            "node_id": private.get("node_id"),
            "inputs": private["inputs"],
            "approvals": [_public(row) for row in approvals],
            "result": await self._decrypt(run, run, "result", "execution_result", execution_id),
        }

    async def events(self, execution_id: str, *, after: int = 0, limit: int = 100) -> list[dict]:
        run = await self.get(execution_id)
        if run is None:
            raise KeyError(execution_id)
        rows = (
            await self.session.execute(
                text("""SELECT * FROM workflow_execution_events
            WHERE execution_id=:id AND seq>:after ORDER BY seq LIMIT :limit"""),
                {"id": run["id"], "after": max(0, after), "limit": max(1, min(limit, 500))},
            )
        ).mappings()
        return [
            await self._decrypt(run, row, "payload", "execution_event", f"{execution_id}:{row['seq']}") for row in rows
        ]

    async def history(
        self,
        *,
        source_type: str,
        source_id: str,
        statuses: list[str] | None = None,
        pending_for_user_id: str | None = None,
        before: tuple[datetime, str] | None = None,
        limit: int = 50,
    ) -> dict:
        clauses = ["r.source_type=:source_type", "r.source_id=:source_id"]
        params: dict = {"source_type": source_type, "source_id": source_id, "limit": max(1, min(limit, 100)) + 1}
        if statuses:
            if not set(statuses) <= RUN_STATUSES:
                raise ValueError("unknown execution status")
            clauses.append("r.status = ANY(:statuses)")
            params["statuses"] = statuses
        if pending_for_user_id:
            clauses.append("""EXISTS (SELECT 1 FROM workflow_execution_approvals a
                WHERE a.execution_id=r.id AND a.approver_user_id=:actor AND a.status='pending'
                AND a.deadline>now())""")
            params["actor"] = uuid.UUID(pending_for_user_id)
        if before:
            clauses.append("(r.created_at,r.id)<(:created_at,:before_id)")
            params.update(created_at=before[0], before_id=uuid.UUID(before[1]))
        rows = list(
            (
                await self.session.execute(
                    text(
                        "SELECT r.* FROM workflow_execution_runs r WHERE "
                        + " AND ".join(clauses)
                        + " ORDER BY r.created_at DESC,r.id DESC LIMIT :limit"
                    ),
                    params,
                )
            ).mappings()
        )
        has_more = len(rows) == params["limit"]
        items = [_public(row) for row in rows[: params["limit"] - 1]]
        pending = {item["id"]: [] for item in items}
        if items:
            approvals = (await self.session.execute(
                text("""SELECT id,execution_id,node_id,approver_user_id,deadline
                FROM workflow_execution_approvals WHERE execution_id=ANY(:ids)
                AND status IN ('pending','decision_requested')
                ORDER BY requested_at,id"""),
                {"ids": [uuid.UUID(item["id"]) for item in items]},
            )).mappings()
            for approval in approvals:
                pending[str(approval["execution_id"])].append(_public(approval))
        for item in items:
            item["pending_approvals"] = pending[item["id"]]
        return {"items": items, "has_more": has_more}
