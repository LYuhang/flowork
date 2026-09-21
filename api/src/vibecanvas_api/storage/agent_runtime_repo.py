"""Durable user defaults and immutable per-Chat Agent Runtime binding."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vibecanvas_api.config import config
from vibecanvas_api.services.agent_runtime.registry import AVAILABLE_RUNTIME_TYPES

from .models import Chat, UserAgentPreference


def validate_user_timezone(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 128:
        raise ValueError("invalid IANA timezone")
    try:
        ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("invalid IANA timezone") from exc
    return normalized


class AgentRuntimeRepo:
    def __init__(self, session: AsyncSession, user_id: str) -> None:
        self._session = session
        self._user_id = user_id

    async def get_preferences(self) -> dict:
        row = await self._session.get(UserAgentPreference, self._user_id)
        configured = tuple(
            runtime
            for runtime in config.agent_runtime_types
            if runtime in AVAILABLE_RUNTIME_TYPES
        )
        default_runtime = configured[0]
        stored_runtime = row.default_runtime_type if row is not None else None
        return {
            "default_runtime_type": (
                stored_runtime if stored_runtime in configured else default_runtime
            ),
            "codex_managed_profile_id": (
                row.codex_managed_profile_id if row is not None else None
            ),
            "preferred_timezone": (
                row.preferred_timezone if row is not None else None
            ),
        }

    async def set_default_runtime_type(self, runtime_type: str) -> dict:
        if (
            runtime_type not in AVAILABLE_RUNTIME_TYPES
            or runtime_type not in config.agent_runtime_types
        ):
            raise ValueError(f"unsupported runtime type: {runtime_type}")
        row = await self._session.get(UserAgentPreference, self._user_id)
        if row is None:
            row = UserAgentPreference(
                user_id=self._user_id,
                default_runtime_type=runtime_type,
            )
            self._session.add(row)
        else:
            row.default_runtime_type = runtime_type
        await self._session.flush()
        return {
            "default_runtime_type": row.default_runtime_type,
            "codex_managed_profile_id": row.codex_managed_profile_id,
        }

    async def set_codex_managed_profile(self, profile_id: str | None) -> dict:
        row = await self._session.get(UserAgentPreference, self._user_id)
        if row is None:
            row = UserAgentPreference(
                user_id=self._user_id,
                default_runtime_type=config.agent_runtime_types[0],
                codex_managed_profile_id=profile_id,
            )
            self._session.add(row)
        else:
            row.codex_managed_profile_id = profile_id
        await self._session.flush()
        return await self.get_preferences()

    async def set_preferred_timezone(self, timezone_name: str) -> dict:
        timezone_name = validate_user_timezone(timezone_name)
        row = await self._session.get(UserAgentPreference, self._user_id)
        if row is None:
            row = UserAgentPreference(
                user_id=self._user_id,
                default_runtime_type=config.agent_runtime_types[0],
                preferred_timezone=timezone_name,
            )
            self._session.add(row)
        else:
            row.preferred_timezone = timezone_name
        await self._session.flush()
        return await self.get_preferences()

    async def bind_chat(
        self,
        chat_id: str,
        *,
        runtime_type: str | None = None,
        user_timezone: str | None = None,
    ) -> dict | None:
        """Atomically bind a Chat once and return the stable binding.

        The row lock is the cross-worker invariant. Competing first-turn
        requests can never initialize two SDK sessions or observe two defaults.
        """
        chat = (
            await self._session.execute(
                select(Chat)
                .where(
                    Chat.chat_id == chat_id,
                    Chat.creator_user_id == self._user_id,
                    Chat.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if chat is None:
            return None
        preference = await self._session.get(
            UserAgentPreference, self._user_id
        )
        if chat.runtime_type is None:
            selected = runtime_type
            if selected is None:
                selected = (await self.get_preferences())["default_runtime_type"]
            if (
                selected not in AVAILABLE_RUNTIME_TYPES
                or selected not in config.agent_runtime_types
            ):
                raise ValueError(f"unsupported runtime type: {selected}")
            chat.runtime_type = selected
            chat.runtime_session_id = f"rt_{selected}_{uuid.uuid4().hex}"
            # Codex assigns its native thread ref when the adapter opens the
            # first turn.
            chat.runtime_state_ref = None
            chat.runtime_version = 1
            configured_timezone = (
                preference.preferred_timezone
                if preference is not None and preference.preferred_timezone
                else user_timezone or "UTC"
            )
            chat.runtime_timezone = validate_user_timezone(configured_timezone)
            chat.runtime_started_at = datetime.now(timezone.utc)
            if user_timezone and (
                preference is None or not preference.preferred_timezone
            ):
                if preference is None:
                    preference = UserAgentPreference(
                        user_id=self._user_id,
                        default_runtime_type=selected,
                    )
                    self._session.add(preference)
                preference.preferred_timezone = validate_user_timezone(user_timezone)
            await self._session.flush()
        elif chat.runtime_type == "codex" and (
            not chat.runtime_timezone or chat.runtime_started_at is None
        ):
            configured_timezone = (
                preference.preferred_timezone
                if preference is not None and preference.preferred_timezone
                else user_timezone or "UTC"
            )
            chat.runtime_timezone = validate_user_timezone(
                configured_timezone
            )
            chat.runtime_started_at = datetime.now(timezone.utc)
            await self._session.flush()
        return self._binding(chat)

    async def get_chat_binding(self, chat_id: str) -> dict | None:
        chat = (
            await self._session.execute(
                select(Chat).where(
                    Chat.chat_id == chat_id,
                    Chat.creator_user_id == self._user_id,
                    Chat.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        return self._binding(chat) if chat is not None else None

    async def set_runtime_state_ref(
        self,
        chat_id: str,
        *,
        runtime_type: str,
        runtime_session_id: str,
        state_ref: str,
        previous_state_ref: str | None = None,
    ) -> dict | None:
        """Persist or compare-and-swap the Runtime-native dialogue reference.

        The row lock makes the first write and an intentional Codex thread
        rotation safe across API workers.  A different value is accepted only
        when the Runtime supplies the exact previously persisted reference;
        stale or unsolicited forks remain invariant violations.
        """
        if not state_ref.strip():
            raise ValueError("runtime state ref is required")
        expected_previous = str(previous_state_ref or "").strip()
        chat = (
            await self._session.execute(
                select(Chat)
                .where(
                    Chat.chat_id == chat_id,
                    Chat.creator_user_id == self._user_id,
                    Chat.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if chat is None:
            return None
        if (
            chat.runtime_type != runtime_type
            or chat.runtime_session_id != runtime_session_id
        ):
            raise ValueError("runtime binding changed during turn")
        if chat.runtime_state_ref is None:
            chat.runtime_state_ref = state_ref
            await self._session.flush()
        elif chat.runtime_state_ref != state_ref:
            if expected_previous and chat.runtime_state_ref == expected_previous:
                chat.runtime_state_ref = state_ref
                await self._session.flush()
            else:
                raise ValueError("runtime state ref conflict")
        return self._binding(chat)

    async def set_runtime_model_selection(
        self,
        chat_id: str,
        *,
        runtime_type: str,
        model_id: str,
        connection_id: str,
        agent_settings: dict,
    ) -> dict | None:
        """Persist the model/settings used by the next accepted Turn.

        Runtime type and the exact credential/account connection remain fixed
        after the first accepted Turn. A user may switch models and reasoning
        effort within that connection between idle Turns. The row lock makes
        the latest accepted selection authoritative for Resume; each AgentRun
        separately stores its immutable execution snapshot.
        """
        if not model_id.strip() or not connection_id.strip():
            raise ValueError("runtime model id is required")
        chat = (
            await self._session.execute(
                select(Chat)
                .where(
                    Chat.chat_id == chat_id,
                    Chat.creator_user_id == self._user_id,
                    Chat.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if chat is None:
            return None
        if chat.runtime_type != runtime_type:
            raise ValueError("runtime binding changed during model selection")
        if (
            chat.runtime_connection_id is not None
            and chat.runtime_connection_id != connection_id
        ):
            raise ValueError("runtime connection is fixed for the Chat")
        normalized_settings = {**agent_settings, "model_id": model_id}
        chat.runtime_connection_id = connection_id
        chat.runtime_model_id = model_id
        chat.runtime_agent_settings = normalized_settings
        await self._session.flush()
        return self._binding(chat)

    @staticmethod
    def _binding(chat: Chat) -> dict:
        return {
            "runtime_type": chat.runtime_type,
            "runtime_session_id": chat.runtime_session_id,
            "runtime_state_ref": chat.runtime_state_ref,
            "runtime_version": chat.runtime_version,
            "runtime_model_id": chat.runtime_model_id,
            "runtime_agent_settings": chat.runtime_agent_settings,
            "runtime_connection_id": chat.runtime_connection_id,
            "runtime_timezone": chat.runtime_timezone,
            "runtime_started_at": chat.runtime_started_at,
        }
