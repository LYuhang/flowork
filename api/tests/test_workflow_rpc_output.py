"""Silent resident output must not exhaust the shared default executor."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import os

import pytest

from vibecanvas_api.services.sandbox.workflow_rpc_slot import WorkflowRpcSlot


@pytest.mark.asyncio
async def test_silent_output_readers_leave_executor_available_and_bound_diagnostics():
    loop = asyncio.get_running_loop()
    previous = loop._default_executor
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    slot = object.__new__(WorkflowRpcSlot)
    slot._booting = True
    slot._startup_output = ""
    writers, readers = [], []
    try:
        for _ in range(6):
            read_fd, write_fd = os.pipe()
            writers.append(write_fd)
            readers.append(asyncio.create_task(slot._discard_output(os.fdopen(read_fd, "rb"))))
        await asyncio.sleep(0.01)
        assert not any(task.done() for task in readers)
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "available"), timeout=1) == "available"
        os.write(writers[0], b"x" * 5000)
        for _ in range(100):
            if len(slot._startup_output) == 4000:
                break
            await asyncio.sleep(0.01)
        assert slot._startup_output == "x" * 4000
        slot._booting = False
        os.write(writers[0], b"business-output-must-not-be-retained")
    finally:
        for fd in writers:
            os.close(fd)
        await asyncio.wait_for(asyncio.gather(*readers), timeout=2)
        loop._default_executor = previous
        executor.shutdown(wait=True)
    assert slot._startup_output == "x" * 4000
