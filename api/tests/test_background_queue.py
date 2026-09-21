from __future__ import annotations

import pytest

from vibecanvas_api.services import background_queue
from vibecanvas_api import background_worker


class _Handle:
    def __init__(self, workflow_id: str):
        self.workflow_id = workflow_id

    def get_workflow_id(self) -> str:
        return self.workflow_id


class _Client:
    def __init__(self):
        self.queues: list[tuple[str, dict]] = []
        self.enqueues: list[tuple[dict, dict]] = []
        self.cancelled: list[tuple[str, bool]] = []

    def register_queue(self, name: str, **kwargs):
        self.queues.append((name, kwargs))

    def enqueue(self, options: dict, payload: dict):
        self.enqueues.append((options, payload))
        return _Handle(options["workflow_id"])

    def enqueue_in_transaction(self, connection, options: dict, payload: dict):
        assert connection == "sync-connection"
        self.enqueues.append((options, payload))
        return _Handle(options["workflow_id"])

    def cancel_workflow(self, workflow_id: str, *, cancel_children: bool):
        self.cancelled.append((workflow_id, cancel_children))

    def destroy(self):
        pass


@pytest.fixture
def client(monkeypatch):
    value = _Client()
    monkeypatch.setattr(background_queue, "_client", value)
    background_queue._registered_queues.clear()
    yield value
    background_queue._registered_queues.clear()
    monkeypatch.setattr(background_queue, "_client", None)


def test_enqueue_uses_job_id_as_dbos_id_and_registers_queue_once(client):
    for _ in range(2):
        result = background_queue.enqueue_background_job(
            "batch_exec",
            job_id="job-1",
            queue="interactive",
            kwargs={"task_id": "task-1"},
        )

    assert result == "job-1"
    assert len(client.queues) == 1
    options, payload = client.enqueues[0]
    assert options == {
        "workflow_name": "batch_exec",
        "queue_name": "interactive",
        "workflow_id": "job-1",
        "application_name": background_queue.BACKGROUND_APPLICATION_NAME,
    }
    assert payload == {"task_id": "task-1"}


def test_unknown_queue_fails_before_enqueue(client):
    with pytest.raises(ValueError, match="unknown background queue"):
        background_queue.enqueue_background_job(
            "batch_exec",
            job_id="job-1",
            queue="missing",
            kwargs={"task_id": "task-1"},
        )
    assert client.enqueues == []


def test_private_business_payload_is_rejected_before_enqueue(client):
    with pytest.raises(ValueError, match="requires only opaque keys"):
        background_queue.enqueue_background_job(
            "deployment_invoke",
            job_id="invoke-1",
            queue="deployments",
            kwargs={"invocation_id": "invoke-1", "inputs": {"secret": "x"}},
        )
    assert client.enqueues == []


def test_worker_dbos_config_uses_observability_log_level(monkeypatch):
    monkeypatch.setattr(
        background_worker.config.observability, "log_level", "warning"
    )
    assert background_worker._dbos_config()["console_log_level"] == "WARNING"


def test_cancel_requests_child_cancellation(client):
    background_queue.cancel_background_job("job-1")
    assert client.cancelled == [("job-1", True)]


@pytest.mark.asyncio
async def test_transactional_enqueue_uses_caller_connection(client):
    class _Session:
        async def connection(self):
            return self

        async def run_sync(self, callback):
            return callback("sync-connection")

    result = await background_queue.enqueue_background_job_in_transaction(
        _Session(),
        "deployment_invoke",
        job_id="invoke-1",
        queue="deployments",
        kwargs={"invocation_id": "invoke-1"},
    )

    assert result == "invoke-1"
    assert client.enqueues[0][0]["workflow_id"] == "invoke-1"
