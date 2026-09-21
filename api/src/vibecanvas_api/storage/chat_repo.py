# -*- coding: utf-8 -*-
"""Postgres-backed chat session and message data access.

Session metadata lives in ``chats``; one row per completed message in
``chat_messages``; each message is persisted when it is itself
complete, with its own short session under DI teardown). The legacy
disk-jsonl/SessionIndex implementation is replaced; the public surface
the FastAPI routes depend on is preserved byte-for-byte:

* ``register_session`` — accepts the route's positional
  ``(scope_id, chat_id, chat_context=...)`` shape as well as the spec's
  ``(scope_id, name=, major_version=, chat_id=)`` shape.
* ``list_sessions`` — returns dicts carrying BOTH ``name`` (spec) and
  ``chat_context``/``created_at`` (legacy route mapper) keys so callers
  stay frozen.
* ``checkpointer_thread_id`` — kept as a ``@staticmethod`` with the
  ``(username, scope_id, chat_id, major_version=0)`` namespaced signature;
  Runtime-native thread-id semantics are owned by the Runtime adapter, not
  T7, and ``context.py``/routes call it statically.

Attachment helpers (``save_attachment``/``add_attachment``/
``resolve_attachment``) and ``prune_empty`` have no production callers
(grep-verified); they are retained as no-op/best-effort shims so the
class surface does not shrink. Attachment/ref storage moves to RefRepo
in T8.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.storage.models import Chat, ChatMcpBinding, ChatMessage
from vibecanvas_api.security.content_encryption import content_encryption_service


def _now():
    return datetime.now(timezone.utc)


class ChatRepo:
    def __init__(self, session: AsyncSession, user_id: str):
        self._s = session
        self._user_id = user_id

    async def _tenant_id(self) -> uuid.UUID:
        value = (
            await self._s.execute(
                text("SELECT current_setting('app.tenant_id', true)")
            )
        ).scalar_one()
        if not value:
            raise RuntimeError("tenant context is required for Chat metadata")
        return uuid.UUID(str(value))

    async def _store_chat_private(
        self,
        chat: Chat,
        *,
        name: str,
        meta: dict,
    ) -> None:
        encrypted = await content_encryption_service().encrypt_json(
            self._s,
            tenant_id=chat.tenant_id,
            resource_type="organization_metadata",
            resource_id=str(chat.tenant_id),
            purpose="chat_metadata",
            record_id=chat.chat_id,
            value={"name": name, "meta": dict(meta)},
        )
        chat.metadata_ciphertext = encrypted.ciphertext
        chat.metadata_nonce = encrypted.nonce
        chat.metadata_key_id = encrypted.key_id
        chat.name = name
        chat.meta = dict(meta)

    async def _materialize_chat_private(self, chat: Chat) -> Chat:
        if (
            chat.metadata_key_id is None
            or not chat.metadata_ciphertext
            or not chat.metadata_nonce
        ):
            raise ValueError("Chat metadata ciphertext is missing")
        value = await content_encryption_service().decrypt_json(
            self._s,
            key_id=chat.metadata_key_id,
            tenant_id=chat.tenant_id,
            resource_type="organization_metadata",
            resource_id=str(chat.tenant_id),
            purpose="chat_metadata",
            record_id=chat.chat_id,
            ciphertext=chat.metadata_ciphertext,
            nonce=chat.metadata_nonce,
        )
        if not isinstance(value, dict):
            raise ValueError("Chat metadata ciphertext must contain an object")
        chat.name = str(value.get("name") or "")
        meta = value.get("meta")
        chat.meta = dict(meta) if isinstance(meta, dict) else {}
        return chat

    async def materialize_session_metadata(self, chat: Chat) -> Chat:
        """Materialize an already-authorized Chat row for host projections."""
        return await self._materialize_chat_private(chat)

    async def commit(self) -> None:
        """Commit the DI session NOW instead of waiting for dependency teardown.

        ``post_message`` needs the freshly-created chat row to be durable BEFORE
        it starts streaming the turn: the history endpoint (a separate request)
        gates on ``list_sessions`` (§5.4 RLS), so an uncommitted chat row makes
        the post-`done` history refetch 404 → the just-finished turn blinks out
        until a later turn's refetch finally sees the committed row."""
        await self._s.commit()

    # ===================================================================
    # Session API
    # ===================================================================

    async def _create_session(self, scope_id: str, name: str,
                              major_version: int, chat_id: str,
                              surface: str = "chat") -> str:
        """Core implementation: create a chat session row, idempotent on
        chat_id. Returns chat_id.

        Preconditions (callers must satisfy):
        - ``chat_id`` is non-empty.
        - ``major_version`` >= 1 (the chats table enforces ``major_version > 0``).
        """
        # A new Chat can be materialized by several entry points (the first
        # attachment, an explicit sandbox warm-up, or the first Turn). Multiple
        # files are also allowed in one composer selection. Serialize that
        # first-create decision across API workers so concurrent requests see
        # the row created by the winner instead of racing into the primary-key
        # constraint and leaking a 500 to the user.
        await self._s.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"chat-create:{chat_id}"},
        )
        existing = (await self._s.execute(
            select(
                Chat.creator_user_id,
                Chat.scope_id,
                Chat.surface,
            ).where(
                Chat.chat_id == chat_id,
                Chat.deleted_at.is_(None),
            )
        )).one_or_none()
        if existing is not None:
            if (
                str(existing.creator_user_id) != str(self._user_id)
                or existing.scope_id != scope_id
                or existing.surface != surface
            ):
                raise LookupError(f"chat {chat_id} not found")
            return chat_id

        tenant_id = await self._tenant_id()
        chat = Chat(
            chat_id=chat_id,
            scope_id=scope_id,
            major_version=major_version,
            creator_user_id=self._user_id,
            tenant_id=tenant_id,
            surface=surface,
        )
        await self._store_chat_private(chat, name=name, meta={})
        self._s.add(chat)
        await self._s.flush()
        return chat_id

    async def register_session(self, scope_id: str, name: str = "",
                               major_version: int = 1,
                               chat_id: str = "",
                               chat_context: str = "",
                               surface: str = "chat") -> str:
        """Create a chat session row; idempotent on chat_id. Returns chat_id.

        Two accepted call shapes, disambiguated by the presence of
        ``chat_context``:

        * Spec/new:  ``register_session(scope_id, name="chat A",
          major_version=1)`` — chat_id auto-generated.
        * Legacy route: ``register_session(scope_id, <chat_id>,
          chat_context=<display name>)`` — the 2nd positional (declared
          ``name``) is actually the frontend-supplied chat_id, and
          ``chat_context`` carries the real display name.  This positional
          inversion is isolated here and does not reach ``_create_session``.

        ``major_version`` is coerced to 1 if falsy (the chats table
        enforces ``major_version > 0``).
        """
        if chat_context:
            # Legacy-route shape: 2nd positional was bound to `name` but
            # actually holds the chat_id; chat_context is the display name.
            chat_id = chat_id or name
            name = chat_context
        return await self._create_session(
            scope_id,
            name or "",
            major_version or 1,
            chat_id or uuid.uuid4().hex[:12],
            surface if surface in {"chat", "browser"} else "chat",
        )

    async def list_sessions(self, scope_id: str,
                            major_version: int = 0,
                            surface: str | None = None) -> list[dict]:
        if not scope_id:
            return []
        q = select(Chat).where(
            Chat.scope_id == scope_id,
            Chat.creator_user_id == self._user_id,
            Chat.deleted_at.is_(None),
        )
        if major_version:
            q = q.where(Chat.major_version == major_version)
        if surface:
            q = q.where(Chat.surface == surface)
        q = q.order_by(Chat.last_message_at.desc().nullslast(),
                       Chat.created_at.desc())
        rows = (await self._s.execute(q)).scalars().all()
        # Carry both the spec key (`name`) and the legacy route-mapper
        # keys (`chat_context`/`created_at`) so callers stay frozen.
        for chat in rows:
            await self._materialize_chat_private(chat)
        return [{"chat_id": c.chat_id, "name": c.name,
                 "chat_context": c.name,
                 "surface": c.surface,
                 "runtime_type": c.runtime_type,
                 "browser_control_status": c.browser_control_status,
                 "major_version": c.major_version,
                 "active_modes": list((c.meta or {}).get("active_modes", [])),
                 "created_at": c.created_at.timestamp()} for c in rows]

    async def list_authorized_sessions(
        self,
        scope_id: str,
        authorized_ids: tuple[str, ...] | list[str] | set[str],
        *,
        major_version: int = 0,
        surface: str | None = None,
    ) -> list[dict]:
        """Intersect OpenFGA ListObjects with tenant-scoped Chat rows.

        Chat is private in V1, but its metadata list still uses the same
        authorization/query shape as every other platform resource. Keeping
        this separate from ``list_sessions`` preserves the low-level
        owner-filtered repository seam used by Runtime internals and tests.
        """
        ids = tuple(dict.fromkeys(str(item) for item in authorized_ids if item))
        if not scope_id or not ids:
            return []
        query = select(Chat).where(
            Chat.scope_id == scope_id,
            Chat.chat_id.in_(ids),
            Chat.deleted_at.is_(None),
        )
        if major_version:
            query = query.where(Chat.major_version == major_version)
        if surface:
            query = query.where(Chat.surface == surface)
        query = query.order_by(
            Chat.last_message_at.desc().nullslast(),
            Chat.created_at.desc(),
        )
        rows = (await self._s.execute(query)).scalars().all()
        for chat in rows:
            await self._materialize_chat_private(chat)
        return [
            {
                "chat_id": chat.chat_id,
                "name": chat.name,
                "chat_context": chat.name,
                "surface": chat.surface,
                "runtime_type": chat.runtime_type,
                "browser_control_status": chat.browser_control_status,
                "major_version": chat.major_version,
                "active_modes": list(
                    (chat.meta or {}).get("active_modes", [])
                ),
                "created_at": chat.created_at.timestamp(),
            }
            for chat in rows
        ]

    async def list_authorized_inventory(
        self,
        authorized_ids: tuple[str, ...] | list[str] | set[str],
    ) -> list[dict]:
        """Return operational Chat metadata without decrypting private data."""
        ids = tuple(dict.fromkeys(str(item) for item in authorized_ids if item))
        if not ids:
            return []
        rows = (
            await self._s.execute(
                select(Chat).where(
                    Chat.chat_id.in_(ids),
                    Chat.deleted_at.is_(None),
                ).order_by(
                    Chat.last_message_at.desc().nullslast(),
                    Chat.created_at.desc(),
                )
            )
        ).scalars().all()
        return [self._inventory_row(chat) for chat in rows]

    async def get_authorized_inventory(self, chat_id: str) -> dict | None:
        """Fetch one content-free row after the caller's Authz decision."""
        chat = (
            await self._s.execute(
                select(Chat).where(
                    Chat.chat_id == chat_id,
                    Chat.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        return self._inventory_row(chat) if chat is not None else None

    @staticmethod
    def _inventory_row(chat: Chat) -> dict:
        return {
            "chat_id": chat.chat_id,
            "scope_id": chat.scope_id,
            "surface": chat.surface,
            "runtime_type": chat.runtime_type,
            "runtime_session_id": chat.runtime_session_id,
            "runtime_state_ref": chat.runtime_state_ref,
            "runtime_version": chat.runtime_version,
            "browser_control_status": chat.browser_control_status,
            "creator_user_id": str(chat.creator_user_id),
            "created_at": chat.created_at.isoformat(),
            "updated_at": chat.updated_at.isoformat(),
            "last_message_at": (
                chat.last_message_at.isoformat()
                if chat.last_message_at is not None else None
            ),
        }

    async def get_browser_binding(self, chat_id: str) -> dict | None:
        chat = (await self._s.execute(
            select(Chat).where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if chat is None:
            return None
        return {
            "chat_id": chat.chat_id,
            "status": chat.browser_control_status,
            "browser_session_id": chat.browser_session_id,
            "browser_session_generation": chat.browser_session_generation,
            "browser_last_event_seq": chat.browser_last_event_seq,
            "browser_lost_at": chat.browser_lost_at.isoformat() if chat.browser_lost_at else None,
        }

    async def get_active_browser_binding_for_user(self) -> dict | None:
        """Return this user's sole active V1 browser-control lease."""
        chat = (await self._s.execute(
            select(Chat)
            .where(
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
                Chat.browser_control_status.in_((
                    "attaching", "attached", "lost",
                )),
            )
            .limit(1)
        )).scalar_one_or_none()
        return await self.get_browser_binding(chat.chat_id) if chat is not None else None

    async def reserve_browser_session(
        self,
        *,
        chat_id: str,
        browser_session_id: str,
        lost_grace_seconds: int,
    ) -> dict:
        """Reserve a browser lease before the extension attaches Debugger.

        This is the first half of the V1 four-step handshake:
        DB CAS -> ``attaching`` -> extension attach -> DB CAS -> ``attached``.
        It prevents the UI from projecting a successful attach before Chrome has
        actually accepted debugger control. V1 exposes one browser-control
        entity per user, so the lease is unique by tenant + creator, not by a
        browser/window id copied out of the extension.
        """
        chat = (await self._s.execute(
            select(Chat)
            .where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            )
            .with_for_update()
        )).scalar_one_or_none()
        if chat is None:
            return {"ok": False, "error_code": "chat_not_found"}
        # The partial unique index is the final database invariant, while this
        # transaction-scoped lock makes competing reservations for two different
        # chats deterministic: the second transaction observes the first lease
        # and returns ``browser_busy`` instead of surfacing an IntegrityError.
        await self._s.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"browser-control:{chat.creator_user_id}"},
        )
        # A disconnected extension keeps its lease briefly so reconnect can
        # restore the same Chat. Once that grace has elapsed, a new Chat must be
        # able to acquire control without relying on a frontend history-list or
        # binding GET to perform lazy cleanup first.
        from datetime import timedelta

        stale_lost = list((await self._s.execute(
            select(Chat)
            .where(
                Chat.creator_user_id == chat.creator_user_id,
                Chat.deleted_at.is_(None),
                Chat.browser_control_status == "lost",
                Chat.browser_lost_at.is_not(None),
                Chat.browser_lost_at
                <= _now() - timedelta(seconds=max(1, lost_grace_seconds)),
            )
            .with_for_update()
        )).scalars().all())
        for stale in stale_lost:
            await self._materialize_chat_private(stale)
            expired_session_id = stale.browser_session_id
            expired_generation = int(stale.browser_session_generation or 0)
            stale.browser_control_status = "inactive"
            stale.browser_session_id = None
            stale.browser_lost_at = None
            stale.browser_last_event_seq = 0
            stale.updated_at = _now()
            meta = dict(stale.meta or {})
            meta.update({
                "browser_last_release_reason": "connection_lost_timeout",
                "browser_last_released_session_id": expired_session_id,
                "browser_last_released_generation": expired_generation,
            })
            await self._store_chat_private(stale, name=stale.name, meta=meta)
        if stale_lost:
            await self._s.flush()
        conflicting = (await self._s.execute(
            select(Chat.chat_id)
            .where(
                Chat.chat_id != chat_id,
                Chat.creator_user_id == chat.creator_user_id,
                Chat.deleted_at.is_(None),
                Chat.browser_control_status.in_(("attaching", "attached", "lost")),
            )
            .limit(1)
        )).scalar_one_or_none()
        if conflicting:
            return {
                "ok": False,
                "error_code": "browser_busy",
                "conflicting_chat_id": conflicting,
            }
        if chat.browser_control_status in {"attaching", "attached", "lost"}:
            if chat.browser_control_status in {"attaching", "attached"}:
                return {
                    "ok": True,
                    "already_attached": chat.browser_control_status == "attached",
                    "already_reserved": chat.browser_control_status == "attaching",
                    "binding": await self.get_browser_binding(chat_id),
                }
            return {
                "ok": False,
                "error_code": (
                    "browser_connection_lost"
                ),
                "binding": await self.get_browser_binding(chat_id),
            }
        now = _now()
        chat.browser_control_status = "attaching"
        chat.browser_session_id = browser_session_id
        chat.browser_session_generation = int(chat.browser_session_generation or 0) + 1
        chat.browser_last_event_seq = 0
        chat.browser_lost_at = None
        chat.updated_at = now
        await self._s.flush()
        return {
            "ok": True,
            "already_attached": False,
            "already_reserved": False,
            "binding": await self.get_browser_binding(chat_id),
        }

    async def confirm_browser_session_attached(
        self,
        *,
        chat_id: str,
        browser_session_id: str,
        browser_session_generation: int | None = None,
    ) -> dict:
        """Advance a reserved browser lease from ``attaching`` to ``attached``."""
        chat = (await self._s.execute(
            select(Chat)
            .where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            )
            .with_for_update()
        )).scalar_one_or_none()
        if chat is None:
            return {"ok": False, "error_code": "chat_not_found"}
        if chat.browser_session_id != browser_session_id:
            return {"ok": False, "error_code": "browser_session_generation_mismatch"}
        if (
            browser_session_generation is not None
            and int(chat.browser_session_generation or 0) != int(browser_session_generation)
        ):
            return {"ok": False, "error_code": "browser_session_generation_mismatch"}
        if chat.browser_control_status == "attached":
            return {"ok": True, "already_attached": True, "binding": await self.get_browser_binding(chat_id)}
        if chat.browser_control_status != "attaching":
            return {
                "ok": False,
                "error_code": "browser_session_state_mismatch",
                "binding": await self.get_browser_binding(chat_id),
            }
        chat.browser_control_status = "attached"
        chat.updated_at = _now()
        await self._s.flush()
        return {"ok": True, "already_attached": False, "binding": await self.get_browser_binding(chat_id)}

    async def release_browser_session(
        self,
        chat_id: str,
        *,
        browser_session_id: str | None = None,
        browser_session_generation: int | None = None,
        event_seq: int = 0,
        reason: str = "released",
    ) -> dict:
        chat = (await self._s.execute(
            select(Chat)
            .where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            )
            .with_for_update()
        )).scalar_one_or_none()
        if chat is None:
            return {"ok": False, "error_code": "chat_not_found"}
        if (
            browser_session_id
            and chat.browser_session_id
            and browser_session_id != chat.browser_session_id
        ):
            return {"ok": False, "error_code": "browser_session_generation_mismatch"}
        if (
            browser_session_generation is not None
            and int(chat.browser_session_generation or 0)
            != int(browser_session_generation)
        ):
            return {"ok": False, "error_code": "browser_session_generation_mismatch"}
        if event_seq and event_seq <= int(chat.browser_last_event_seq or 0):
            return {
                "ok": True,
                "ignored_stale_event": True,
                "binding": await self.get_browser_binding(chat_id),
            }
        chat.browser_control_status = "inactive"
        chat.browser_session_id = None
        chat.browser_lost_at = None
        chat.browser_last_event_seq = 0
        chat.updated_at = _now()
        await self._materialize_chat_private(chat)
        meta = dict(chat.meta or {})
        meta["browser_last_release_reason"] = reason
        if browser_session_id:
            meta["browser_last_released_session_id"] = browser_session_id
        if browser_session_generation is not None:
            meta["browser_last_released_generation"] = int(browser_session_generation)
        if event_seq:
            meta["browser_last_release_event_seq"] = int(event_seq)
        await self._store_chat_private(chat, name=chat.name, meta=meta)
        await self._s.flush()
        return {"ok": True, "binding": await self.get_browser_binding(chat_id)}

    async def mark_browser_lost(
        self,
        *,
        chat_id: str,
        browser_session_id: str,
        browser_session_generation: int | None = None,
        event_seq: int = 0,
        reason: str = "connection_lost",
    ) -> dict:
        chat = (await self._s.execute(
            select(Chat)
            .where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            )
            .with_for_update()
        )).scalar_one_or_none()
        if chat is None:
            return {"ok": False, "error_code": "chat_not_found"}
        if chat.browser_session_id != browser_session_id:
            return {"ok": False, "error_code": "browser_session_generation_mismatch"}
        if (
            browser_session_generation is not None
            and int(chat.browser_session_generation or 0)
            != int(browser_session_generation)
        ):
            return {"ok": False, "error_code": "browser_session_generation_mismatch"}
        if event_seq and event_seq <= int(chat.browser_last_event_seq or 0):
            return {"ok": True, "ignored_stale_event": True, "binding": await self.get_browser_binding(chat_id)}
        chat.browser_control_status = "lost"
        chat.browser_lost_at = _now()
        chat.browser_last_event_seq = max(int(chat.browser_last_event_seq or 0), int(event_seq or 0))
        await self._materialize_chat_private(chat)
        meta = dict(chat.meta or {})
        meta["browser_lost_reason"] = reason
        await self._store_chat_private(chat, name=chat.name, meta=meta)
        chat.updated_at = _now()
        await self._s.flush()
        return {"ok": True, "binding": await self.get_browser_binding(chat_id)}

    async def reconcile_browser_session_snapshot(
        self,
        *,
        chat_id: str,
        browser_session_id: str,
        browser_session_generation: int,
        event_seq: int,
        controlled: bool,
        reason: str = "reconnect_snapshot",
    ) -> dict:
        """Reconcile durable lease state from an extension reconnect snapshot."""
        chat = (await self._s.execute(
            select(Chat)
            .where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            )
            .with_for_update()
        )).scalar_one_or_none()
        if chat is None:
            return {"ok": False, "error_code": "chat_not_found"}
        if (
            chat.browser_session_id != browser_session_id
            or int(chat.browser_session_generation or 0)
            != int(browser_session_generation)
        ):
            return {"ok": False, "error_code": "browser_session_generation_mismatch"}
        if event_seq and event_seq <= int(chat.browser_last_event_seq or 0):
            return {
                "ok": True,
                "ignored_stale_event": True,
                "binding": await self.get_browser_binding(chat_id),
            }
        if not controlled:
            return await self.release_browser_session(
                chat_id,
                browser_session_id=browser_session_id,
                browser_session_generation=browser_session_generation,
                event_seq=event_seq,
                reason=reason,
            )
        if chat.browser_control_status not in {"attaching", "attached", "lost"}:
            return {"ok": False, "error_code": "browser_session_state_mismatch"}

        chat.browser_control_status = "attached"
        chat.browser_lost_at = None
        chat.browser_last_event_seq = max(
            int(chat.browser_last_event_seq or 0),
            int(event_seq or 0),
        )
        chat.updated_at = _now()
        await self._materialize_chat_private(chat)
        meta = dict(chat.meta or {})
        meta["browser_last_snapshot_reason"] = reason
        await self._store_chat_private(chat, name=chat.name, meta=meta)
        await self._s.flush()
        return {"ok": True, "binding": await self.get_browser_binding(chat_id)}

    async def expire_stale_browser_lost_session(
        self,
        chat_id: str,
        *,
        grace_seconds: int,
    ) -> dict | None:
        """Lazily release a lost browser lease after its reconnect grace.

        This lock-and-check is safe across API workers. The generation remains
        monotonic on the Chat row, so a delayed snapshot/event from the expired
        extension cannot revive or release a future session.
        """
        from datetime import timedelta

        chat = (await self._s.execute(
            select(Chat)
            .where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            )
            .with_for_update()
        )).scalar_one_or_none()
        if chat is None:
            return None
        if (
            chat.browser_control_status != "lost"
            or chat.browser_lost_at is None
            or chat.browser_lost_at > _now() - timedelta(seconds=max(1, grace_seconds))
        ):
            return await self.get_browser_binding(chat_id)

        expired_session_id = chat.browser_session_id
        expired_generation = int(chat.browser_session_generation or 0)
        chat.browser_control_status = "inactive"
        chat.browser_session_id = None
        chat.browser_lost_at = None
        chat.browser_last_event_seq = 0
        chat.updated_at = _now()
        await self._materialize_chat_private(chat)
        meta = dict(chat.meta or {})
        meta.update({
            "browser_last_release_reason": "connection_lost_timeout",
            "browser_last_released_session_id": expired_session_id,
            "browser_last_released_generation": expired_generation,
        })
        await self._store_chat_private(chat, name=chat.name, meta=meta)
        await self._s.flush()
        return await self.get_browser_binding(chat_id)

    async def expire_stale_browser_lost_sessions(
        self,
        *,
        scope_id: str,
        surface: str | None,
        grace_seconds: int,
    ) -> int:
        """Expire stale lost leases visible in one chat list projection."""
        from datetime import timedelta

        q = select(Chat.chat_id).where(
            Chat.scope_id == scope_id,
            Chat.creator_user_id == self._user_id,
            Chat.deleted_at.is_(None),
            Chat.browser_control_status == "lost",
            Chat.browser_lost_at.is_not(None),
            Chat.browser_lost_at <= _now() - timedelta(seconds=max(1, grace_seconds)),
        )
        if surface in {"chat", "browser"}:
            q = q.where(Chat.surface == surface)
        chat_ids = list((await self._s.execute(q)).scalars().all())
        for stale_chat_id in chat_ids:
            await self.expire_stale_browser_lost_session(
                stale_chat_id,
                grace_seconds=grace_seconds,
            )
        return len(chat_ids)

    async def update_session_name_if_default(self, chat_id: str, name: str) -> bool:
        """Replace a draft/default chat title with the first real user message."""
        title = (name or "").strip()
        if not title:
            return False
        chat = (await self._s.execute(
            select(Chat).where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if chat is None:
            return False
        await self._materialize_chat_private(chat)
        if chat.name.strip().lower() not in {"", "new chat"}:
            return False
        await self._store_chat_private(chat, name=title, meta=chat.meta)
        await self._s.flush()
        return True

    async def rename_session(
        self,
        scope_id: str,
        chat_id: str,
        name: str,
    ) -> dict | None:
        """Rename one owned Chat while preserving encrypted private metadata."""
        chat = (await self._s.execute(
            select(Chat).where(
                Chat.scope_id == scope_id,
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            ).with_for_update()
        )).scalar_one_or_none()
        if chat is None:
            return None
        await self._materialize_chat_private(chat)
        await self._store_chat_private(chat, name=name, meta=chat.meta)
        chat.updated_at = _now()
        await self._s.flush()
        return {
            "chat_id": chat.chat_id,
            "scope_id": chat.scope_id,
            "surface": chat.surface,
            "chat_context": name,
            "created_at": str(chat.created_at.timestamp()),
            "browser_control_status": chat.browser_control_status,
            "runtime_type": chat.runtime_type,
        }

    # ===================================================================
    # `/command` active-modes (Design §3) — persisted in chats.meta
    # ===================================================================

    async def get_active_modes(self, chat_id: str) -> set[str]:
        """Read the sticky ``active_modes`` set persisted for this chat.

        Base == empty set (no entry). Returns ``set()`` for an unknown chat or a
        chat with no meta written yet.
        """
        chat = (await self._s.execute(
            select(Chat).where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if chat is None:
            return set()
        await self._materialize_chat_private(chat)
        return set(chat.meta.get("active_modes", []))

    async def set_active_modes(self, chat_id: str, modes: set[str]) -> None:
        """Persist ``active_modes`` (sorted list) into ``meta``, preserving any
        other meta keys. No-op-safe on an unknown chat_id (UPDATE matches 0 rows).
        """
        chat = (await self._s.execute(
            select(Chat).where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            ).with_for_update()
        )).scalar_one_or_none()
        if chat is None:
            return
        await self._materialize_chat_private(chat)
        new_meta = dict(chat.meta or {})
        new_meta["active_modes"] = sorted(modes)
        await self._store_chat_private(chat, name=chat.name, meta=new_meta)
        await self._s.flush()

    async def get_active_diagram(self, chat_id: str) -> dict | None:
        """Return the latest trusted Active Diagram Context for this Chat.

        Older rows stored the DiagramRef directly.  Preserve that read shape as
        a compatibility input and normalize it at the Runtime instruction
        boundary rather than making an existing Chat lose its active diagram.
        """
        chat = (await self._s.execute(
            select(Chat).where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if chat is None:
            return None
        await self._materialize_chat_private(chat)
        value = chat.meta.get("active_diagram")
        return dict(value) if isinstance(value, dict) else None

    async def set_active_diagram(
        self,
        chat_id: str,
        file_ref: dict,
    ) -> None:
        """Persist the ordinary VFS file currently presented as a Diagram.

        This is editor-resume context, not Diagram storage. The source remains
        an ordinary ``/data`` file and ``revision`` is the generic VFS content
        revision used by every Preview.
        """
        allowed = {"path", "revision", "source_hash"}
        projection = {
            key: value
            for key, value in file_ref.items()
            if key in allowed and isinstance(value, str) and value
        }
        if set(projection) != allowed:
            raise ValueError("active Diagram file reference is incomplete")
        chat = (await self._s.execute(
            select(Chat).where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            ).with_for_update()
        )).scalar_one_or_none()
        if chat is None:
            raise LookupError("Chat not found")
        await self._materialize_chat_private(chat)
        new_meta = dict(chat.meta or {})
        new_meta["active_diagram"] = {"file_ref": projection}
        await self._store_chat_private(chat, name=chat.name, meta=new_meta)
        await self._s.flush()

    async def deactivate_active_mode(
        self,
        chat_id: str,
        mode: str,
        *,
        actor_user_id: str,
    ) -> set[str] | None:
        """Remove one sticky command and retain a bounded audit event."""
        chat = (await self._s.execute(
            select(Chat).where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            ).with_for_update()
        )).scalar_one_or_none()
        if chat is None:
            return None
        await self._materialize_chat_private(chat)
        new_meta = dict(chat.meta or {})
        modes = set(new_meta.get("active_modes") or [])
        if mode in modes:
            modes.remove(mode)
            events = list(new_meta.get("command_events") or [])[-99:]
            events.append({
                "type": "deactivated",
                "command": mode,
                "actor_user_id": actor_user_id,
                "created_at": _now().isoformat(),
            })
            new_meta["command_events"] = events
        new_meta["active_modes"] = sorted(modes)
        await self._store_chat_private(chat, name=chat.name, meta=new_meta)
        await self._s.flush()
        return modes

    async def get_mcp_selection(self, chat_id: str) -> dict | None:
        chat = (await self._s.execute(
            select(Chat).where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if chat is None:
            return None
        ids = (await self._s.execute(
            select(ChatMcpBinding.mcp_server_id)
            .where(ChatMcpBinding.chat_id == chat_id)
            .order_by(ChatMcpBinding.mcp_server_id)
        )).scalars().all()
        return {
            "mcp_server_ids": [str(item) for item in ids],
            "mcp_config_revision": int(chat.mcp_config_revision or 0),
        }

    async def set_mcp_selection(
        self,
        chat_id: str,
        *,
        mcp_server_ids: list[uuid.UUID],
        expected_revision: int,
    ) -> dict:
        """CAS-update one Chat's complete selected custom-MCP set."""
        chat = (await self._s.execute(
            select(Chat).where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            ).with_for_update()
        )).scalar_one_or_none()
        if chat is None:
            return {"ok": False, "error_code": "chat_not_found"}
        current_ids = set((await self._s.execute(
            select(ChatMcpBinding.mcp_server_id).where(
                ChatMcpBinding.chat_id == chat_id
            )
        )).scalars().all())
        desired_ids = set(mcp_server_ids)
        current_revision = int(chat.mcp_config_revision or 0)
        if current_revision != expected_revision and current_ids != desired_ids:
            return {
                "ok": False,
                "error_code": "mcp_config_revision_conflict",
                "mcp_server_ids": sorted(map(str, current_ids)),
                "mcp_config_revision": current_revision,
            }
        if current_ids != desired_ids:
            await self._s.execute(
                delete(ChatMcpBinding).where(ChatMcpBinding.chat_id == chat_id)
            )
            for server_id in sorted(desired_ids, key=str):
                self._s.add(ChatMcpBinding(
                    chat_id=chat_id,
                    mcp_server_id=server_id,
                    tenant_id=chat.tenant_id,
                ))
            current_revision += 1
            chat.mcp_config_revision = current_revision
            chat.updated_at = _now()
            await self._s.flush()
        return {
            "ok": True,
            "mcp_server_ids": sorted(map(str, desired_ids)),
            "mcp_config_revision": current_revision,
        }

    async def get_current_workflow_id(self, chat_id: str) -> str | None:
        """Read the real workflow currently associated with this chat.

        General Chat sessions have their own internal workspace scope for
        checkpoint/VFS/sandbox state. ``current_workflow_id`` is the optional
        user-visible workflow that build tools should inspect and mutate after
        `/workflow` + set/create.
        """
        chat = (await self._s.execute(
            select(Chat).where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if chat is None:
            return None
        await self._materialize_chat_private(chat)
        value = chat.meta.get("current_workflow_id")
        return value if isinstance(value, str) and value else None

    async def get_platform_context_binding(self, chat_id: str) -> dict | None:
        """Return the backend-owned context a Platform MCP may bind to.

        Keeping this projection in ``ChatRepo`` prevents Platform MCP code from
        depending on ORM models or duplicating Chat ownership/deletion
        predicates. ``carrier_scope_id`` is intentionally not the Chat's
        sandbox/VFS workspace id; the latter is derived from the authenticated
        user and Chat identity by the platform boundary.
        """
        chat = (await self._s.execute(
            select(Chat).where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if chat is None:
            return None
        await self._materialize_chat_private(chat)
        meta = dict(chat.meta or {})
        current_workflow_id = meta.get("current_workflow_id")
        return {
            "chat_id": chat_id,
            "carrier_scope_id": chat.scope_id,
            "runtime_session_id": chat.runtime_session_id,
            "runtime_type": chat.runtime_type,
            "current_workflow_id": (
                current_workflow_id
                if isinstance(current_workflow_id, str) and current_workflow_id
                else None
            ),
        }

    async def set_current_workflow_id(self, chat_id: str, wf_id: str | None) -> None:
        """Persist the chat's real workflow binding in ``meta``."""
        chat = (await self._s.execute(
            select(Chat).where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            ).with_for_update()
        )).scalar_one_or_none()
        if chat is None:
            return
        await self._materialize_chat_private(chat)
        new_meta = dict(chat.meta or {})
        if wf_id:
            new_meta["current_workflow_id"] = wf_id
        else:
            new_meta.pop("current_workflow_id", None)
        await self._store_chat_private(chat, name=chat.name, meta=new_meta)
        await self._s.flush()

    @staticmethod
    def checkpointer_thread_id(
        username: str, scope_id: str, chat_id: str, major_version: int = 0,
    ) -> str:
        """Namespaced Runtime thread identifier.

        Kept as the legacy ``@staticmethod`` (context.py + routes call it
        without an instance). The thread-id <-> Postgres-checkpointer
        binding is owned by T11; T7 must not change this shape.
        """
        if major_version:
            return f"{username}__{scope_id}__v{major_version}__{chat_id}"
        return f"{username}__{scope_id}__{chat_id}"

    async def drop_session(self, chat_id: str) -> None:
        # Soft-delete the chat row (spec-intended — DO NOT hard-delete).
        # Because the chat is only soft-deleted, the ``chat_messages`` ON
        # DELETE CASCADE FK never fires, so those rows must be removed
        # explicitly in the SAME transaction or they leak orphaned forever
        # The message cascade removes owned rows. The ``refs`` table was
        # dropped in VFS 2b-3, so there is no ref-cascade to handle here.
        result = await self._s.execute(
            update(Chat).where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            )
            .values(deleted_at=_now()))
        if result.rowcount:
            await self._s.execute(
                delete(ChatMessage).where(ChatMessage.chat_id == chat_id))
        await self._s.flush()

    async def drop_authorized_session(self, chat_id: str) -> None:
        """Soft-delete a Chat after an explicit external DELETE decision."""
        result = await self._s.execute(
            update(Chat).where(
                Chat.chat_id == chat_id,
                Chat.deleted_at.is_(None),
            ).values(deleted_at=_now())
        )
        if result.rowcount:
            await self._s.execute(
                delete(ChatMessage).where(ChatMessage.chat_id == chat_id)
            )
        await self._s.flush()

    async def prune_empty(self, scope_id: str, checkpointer,
                          keep_chat_id: str = "") -> list[str]:
        """Soft-delete sessions with no checkpoint state. Best-effort.

        Mirrors the legacy return shape (list of pruned chat_ids). No
        production caller (grep-verified); retained for surface parity.
        """
        sessions = await self.list_sessions(scope_id)
        pruned: list[str] = []
        for s in sessions:
            cid = s["chat_id"]
            if cid == keep_chat_id:
                continue
            tid = self.checkpointer_thread_id(self._user_id, scope_id, cid)
            if checkpointer.get({"configurable": {"thread_id": tid}}) is None:
                await self.drop_session(cid)
                pruned.append(cid)
        return pruned

    # ===================================================================
    # Message API: one row per completed message.
    # ===================================================================

    async def persist_message(self, chat_id: str, msg: dict) -> None:
        """Persist one completed product message, idempotent by message_id."""
        chat = (await self._s.execute(
            select(Chat).where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            ).with_for_update()
        )).scalar_one_or_none()
        if chat is None:
            raise LookupError(f"chat {chat_id} not found")
        message_id = str(msg.get("message_id") or "")
        if not message_id:
            raise ValueError("completed chat message requires message_id")
        content = msg.get("content")
        if not isinstance(content, dict):
            raise ValueError("chat message content must be a structured object")
        meta = msg.get("meta", {})
        crypto = content_encryption_service()
        encrypted = await crypto.encrypt_json(
            self._s,
            tenant_id=chat.tenant_id,
            resource_type="chat",
            resource_id=chat_id,
            purpose="chat_message",
            record_id=message_id,
            value={"content": content, "meta": meta},
        )
        await self._s.execute(
            pg_insert(ChatMessage)
            .values(
                message_id=message_id,
                chat_id=chat_id,
                turn_id=msg.get("turn_id"),
                role=msg["role"],
                content_ciphertext=encrypted.ciphertext,
                content_nonce=encrypted.nonce,
                content_key_id=encrypted.key_id,
            )
            .on_conflict_do_nothing(
                index_elements=["chat_id", "message_id"],
            )
        )
        await self._s.execute(
            update(Chat).where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            )
            .values(last_message_at=_now()))
        await self._s.flush()

    async def list_message_page(
        self,
        chat_id: str,
        *,
        limit: int,
        offset: int = 0,
        tail: bool = False,
        before_turn_id: str | None = None,
    ) -> tuple[list[dict], int, int]:
        """Read and decrypt only one requested transcript window."""
        owned = (await self._s.execute(
            select(Chat.chat_id).where(
                Chat.chat_id == chat_id,
                Chat.creator_user_id == self._user_id,
                Chat.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if owned is None:
            return [], 0, 0
        criteria = [ChatMessage.chat_id == chat_id]
        if before_turn_id:
            boundary_id = (
                await self._s.execute(
                    select(ChatMessage.id)
                    .where(
                        ChatMessage.chat_id == chat_id,
                        ChatMessage.turn_id == before_turn_id,
                    )
                    .order_by(ChatMessage.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if boundary_id is not None:
                criteria.append(ChatMessage.id < boundary_id)
        total = int((await self._s.execute(
            select(func.count()).select_from(ChatMessage).where(*criteria)
        )).scalar_one())
        effective_offset = (
            max(total - limit, 0)
            if tail
            else min(max(offset, 0), total)
        )
        if tail:
            # A tail read is the hot path when a user opens Chat history. Using
            # OFFSET(total - limit) makes PostgreSQL walk every older row before
            # it can return the recent messages. Read the indexed tail in
            # descending order and reverse the small result window in memory.
            q = (
                select(ChatMessage)
                .where(*criteria)
                .order_by(ChatMessage.ts.desc(), ChatMessage.id.desc())
                .limit(limit)
            )
            rows = list(reversed((await self._s.execute(q)).scalars().all()))
        else:
            q = (
                select(ChatMessage)
                .where(*criteria)
                .order_by(ChatMessage.ts, ChatMessage.id)
                .offset(effective_offset)
                .limit(limit)
            )
            rows = list((await self._s.execute(q)).scalars().all())
        result: list[dict] = []
        crypto = content_encryption_service()
        for message in rows:
            if (
                message.content_key_id is None
                or not message.content_ciphertext
                or not message.content_nonce
            ):
                raise ValueError("chat message ciphertext is missing")
            envelope = await crypto.decrypt_json(
                self._s,
                key_id=message.content_key_id,
                tenant_id=message.tenant_id,
                resource_type="chat",
                resource_id=chat_id,
                purpose="chat_message",
                record_id=message.message_id,
                ciphertext=message.content_ciphertext,
                nonce=message.content_nonce,
            )
            if not isinstance(envelope, dict):
                raise ValueError("decrypted chat message must be an object")
            content = envelope.get("content", {})
            meta = envelope.get("meta", {})
            result.append({
                "id": message.id,
                "message_id": message.message_id,
                "role": message.role,
                "content": content,
                "turn_id": message.turn_id,
                "meta": meta,
                "ts": message.ts.timestamp(),
            })
        return result, total, effective_offset

    async def list_messages(
        self, chat_id: str, *, before_turn_id: str | None = None,
    ) -> list[dict]:
        """Compatibility full-history reader for Runtime and test callers."""
        page, _total, _offset = await self.list_message_page(
            chat_id,
            limit=2_147_483_647,
            before_turn_id=before_turn_id,
        )
        return page

    async def set_todo_items(self, chat_id: str, items: list[dict]) -> None:
        """Persist the Runtime-neutral Todo snapshot on the authoritative Chat.

        A Chat's Runtime binding is immutable after its first Turn, so keeping
        the snapshot and its source session on the same locked row gives both
        Runtime adapters one ordered state stream without a second ownership
        graph. ``todo_revision`` is incremented for every full snapshot.
        """
        chat = (
            await self._s.execute(
                select(Chat).where(
                    Chat.chat_id == chat_id,
                    Chat.creator_user_id == self._user_id,
                    Chat.deleted_at.is_(None),
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if chat is None:
            raise LookupError(f"chat {chat_id} not found")
        await self._materialize_chat_private(chat)
        meta = dict(chat.meta or {})
        revision = int(meta.get("todo_revision") or 0) + 1
        await self._store_chat_private(chat, name=chat.name, meta={
            **meta,
            "todo_items": list(items),
            "todo_revision": revision,
            "todo_runtime_type": chat.runtime_type,
            "todo_runtime_session_id": chat.runtime_session_id,
        })
        await self._s.flush()

    async def get_todo_items(self, chat_id: str) -> list[dict]:
        return list((await self.get_todo_state(chat_id))["items"])

    async def get_todo_state(self, chat_id: str) -> dict:
        """Return one backend-owned Todo snapshot and its Runtime binding."""
        chat = (
            await self._s.execute(
                select(Chat).where(
                    Chat.chat_id == chat_id,
                    Chat.creator_user_id == self._user_id,
                    Chat.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if chat is None:
            raise LookupError(f"chat {chat_id} not found")
        await self._materialize_chat_private(chat)
        raw_meta = dict(chat.meta or {})
        items = raw_meta.get("todo_items")
        return {
            "items": list(items) if isinstance(items, list) else [],
            "revision": int(raw_meta.get("todo_revision") or 0),
            "runtime_type": chat.runtime_type,
            "runtime_session_id": chat.runtime_session_id,
        }

    # Legacy aliases — old ChatStore-style names. Kept so any caller that
    # still uses the disk-era names binds to the Postgres backend.
    async def append_message(self, wf_id: str, chat_id: str,
                             message: dict) -> None:
        await self.persist_message(chat_id, message)

    async def load_session(self, wf_id: str, chat_id: str) -> list[dict]:
        return await self.list_messages(chat_id)

    # ===================================================================
    # Attachment API — storage moves to RefRepo (T8); shims for surface
    # ===================================================================

    def save_attachment(self, wf_id: str, src_path, original_name: str = ""):
        raise NotImplementedError(
            "attachment storage moved to RefRepo")

    def add_attachment(self, wf_id: str, chat_id: str, src_path: str,
                        original_name: str = ""):
        raise NotImplementedError(
            "attachment storage moved to RefRepo")

    def resolve_attachment(self, wf_id: str, sha256: str):
        raise NotImplementedError(
            "attachment storage moved to RefRepo")
