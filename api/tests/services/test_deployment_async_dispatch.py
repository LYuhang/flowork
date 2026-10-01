"""Waiting deployments must not consume the shared executor thread budget."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from vibecanvas_api.services import workflow_runner


def test_waiting_deployments_leave_threads_available_for_new_admissions(monkeypatch):
    async def scenario():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        entered = set()
        all_entered = asyncio.Event()
        finish = asyncio.Event()

        async def execute(**kwargs):
            entered.add(kwargs["run_id"])
            if len(entered) == 3:
                all_entered.set()
            await finish.wait()
            return {"final_outputs": {}, "error_dict": {}, "execution_time": 1}

        monkeypatch.setattr(workflow_runner.config, "sandbox_service_mode", "service")
        monkeypatch.setattr(workflow_runner, "_prepare_service_workflow", lambda **kwargs: (execute, kwargs))
        calls = [asyncio.create_task(workflow_runner.run_workflow_sandboxed_async(
            workflow_id="workflow", workflow_dict={}, inputs={}, tenant_id="tenant",
            user_id="user", run_id=str(index), deployment_id="deployment", revision_id="revision",
        )) for index in range(3)]
        try:
            await asyncio.wait_for(all_entered.wait(), timeout=2)
            assert await asyncio.wait_for(asyncio.to_thread(lambda: "available"), timeout=1) == "available"
            assert all(not call.done() for call in calls)
        finally:
            finish.set()
            await asyncio.gather(*calls)

    asyncio.run(scenario())
