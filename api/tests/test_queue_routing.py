"""Deployments T3 — route_for + cluster_role config."""
from __future__ import annotations

import uuid

import pytest

from vibecanvas_api.config import config
from vibecanvas_api.services.background_queue import QUEUE_SPECS
from vibecanvas_api.services.queue_routing import route_for


def test_deployment_invoke_routes_to_deployments_queue():
    assert route_for("deployment_invoke") == "deployments"


def test_batch_exec_routes_to_interactive_queue():
    assert route_for("batch_exec") == "interactive"


def test_agent_turn_routes_to_interactive_queue():
    assert route_for("agent_turn") == "interactive"


def test_unknown_kind_raises():
    with pytest.raises(ValueError, match="Unknown task_kind"):
        route_for("not_a_kind")


def test_route_for_accepts_optional_deployment_id():
    """Forward-compat: ``deployment_id`` is accepted but ignored today."""
    assert route_for("deployment_invoke", uuid.uuid4()) == "deployments"


def test_cluster_role_default_is_monolith():
    # ``config.cluster_role`` lives on the top-level ``AppConfig``.
    assert config.cluster_role == "monolith"


def test_background_queues_are_explicit_and_bounded():
    assert set(QUEUE_SPECS) == {
        "interactive", "deployments", "kb_indexing", "control", "maintenance",
    }
    assert all(spec.worker_concurrency >= 1 for spec in QUEUE_SPECS.values())
