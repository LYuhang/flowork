"""Runtime-neutral lifecycle contract for interactive sandbox sessions.

The sandbox daemon is the only owner of these states. Agent Runtime adapters
may persist their own state or supply authentication, but they never advance a
TTL or transition the containing sandbox session themselves.
"""

from __future__ import annotations

from enum import StrEnum


class SessionLifecycleState(StrEnum):
    """Stable states persisted only in sandboxd memory/status projections."""

    WARM = "warm"
    HIBERNATING = "hibernating"
    HIBERNATED = "hibernated"
    RESTORING = "restoring"
    RELEASING = "releasing"
    SNAPSHOT_FAILED = "snapshot_failed"
    CLOSED = "closed"


class SnapshotKind(StrEnum):
    """Separate reusable clean baselines from workspace-owned hibernation state."""

    BASELINE = "baseline"
    SESSION_HIBERNATION = "session_hibernation"


_ALLOWED_TRANSITIONS: dict[SessionLifecycleState, frozenset[SessionLifecycleState]] = {
    SessionLifecycleState.WARM: frozenset({
        SessionLifecycleState.HIBERNATING,
        SessionLifecycleState.RELEASING,
    }),
    SessionLifecycleState.HIBERNATING: frozenset({
        SessionLifecycleState.WARM,
        SessionLifecycleState.HIBERNATED,
        SessionLifecycleState.SNAPSHOT_FAILED,
        SessionLifecycleState.RELEASING,
    }),
    SessionLifecycleState.HIBERNATED: frozenset({
        SessionLifecycleState.RESTORING,
        SessionLifecycleState.RELEASING,
    }),
    SessionLifecycleState.RESTORING: frozenset({
        SessionLifecycleState.WARM,
        SessionLifecycleState.SNAPSHOT_FAILED,
        SessionLifecycleState.RELEASING,
    }),
    SessionLifecycleState.SNAPSHOT_FAILED: frozenset({
        SessionLifecycleState.RELEASING,
    }),
    SessionLifecycleState.RELEASING: frozenset({
        SessionLifecycleState.CLOSED,
    }),
    SessionLifecycleState.CLOSED: frozenset(),
}


def validate_lifecycle_transition(
    current: SessionLifecycleState | str,
    target: SessionLifecycleState | str,
) -> tuple[SessionLifecycleState, SessionLifecycleState]:
    """Validate and normalize one daemon-owned lifecycle transition."""

    source = SessionLifecycleState(current)
    destination = SessionLifecycleState(target)
    if destination == source:
        return source, destination
    if destination not in _ALLOWED_TRANSITIONS[source]:
        raise RuntimeError(
            f"invalid sandbox lifecycle transition: {source.value} -> "
            f"{destination.value}"
        )
    return source, destination


__all__ = [
    "SessionLifecycleState",
    "SnapshotKind",
    "validate_lifecycle_transition",
]
