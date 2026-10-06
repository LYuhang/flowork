"""CLI-owned execution using the engine runtime in this process, without RPC."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Callable
from uuid import UUID, uuid4, uuid5


async def run_rows(*, workflow: dict, rows: list[dict], concurrency: int,
                   output, events, status: Callable[[dict], None],
                   context_factory: Callable[[int], dict], node_id: str | None = None,
                   approval_handler=None, event_sink=None, run_id=None):
    from vibecanvas_engine.runtime.executions import WorkflowRuntime

    if type(concurrency) is not int or concurrency < 1:
        raise ValueError('concurrency must be a positive integer')
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError('inputs must be a non-empty list of objects')
    run_namespace = UUID(run_id) if run_id else uuid4()
    capacity = min(concurrency, len(rows), 16)
    runtime = WorkflowRuntime(capacity=capacity)
    runtime.install('local', workflow, node_id=node_id)
    completed = failed = 0
    started = time.monotonic()
    iterator = iter(enumerate(rows))
    indexes = {}

    def report(state):
        status({'status': state, 'total': len(rows), 'completed': completed,
                'failed': failed, 'execution_time': time.monotonic() - started})

    async def worker():
        nonlocal completed, failed
        for index, inputs in iterator:
            invocation_id = str(uuid5(run_namespace, str(index)))
            runtime.invoke(invocation_id, 'local', inputs, context_factory(index))
            indexes[invocation_id] = index
            execution = runtime.executions[invocation_id]
            cursor, result = 0, None
            model_calls = {}
            approval_tasks = []
            try:
                while result is None:
                    # This awaits an in-process asyncio.Condition. No network,
                    # host scheduler, lease renewal or external progress query.
                    response = await runtime.events(invocation_id, after=cursor, wait_seconds=25)
                    for event in response['events']:
                        events.write(json.dumps({'index': index, **event}, ensure_ascii=False, default=str) + '\n')
                        events.flush()
                        if event_sink is not None:
                            await event_sink(runtime, invocation_id, index, event)
                        event_node_id = event.get('node_id')
                        if event_node_id and event.get('model_calls'):
                            model_calls.setdefault(event_node_id, []).extend(event['model_calls'])
                        if event['type'] == 'approval_requested':
                            if approval_handler is None:
                                raise RuntimeError('Local approval service is not configured')
                            approval_tasks.append(asyncio.create_task(
                                approval_handler(runtime, invocation_id, index, event)))
                        if event['type'] == 'result':
                            result = event
                        cursor = event['seq']
                    for task in approval_tasks:
                        if task.done():
                            task.result()
                    if result is not None:
                        await execution.task
                    runtime.acknowledge(invocation_id, cursor)
                node_outputs = result.get('final_outputs') or {}
                row_status = result['status']
                record = {'index': index, 'input': inputs,
                          'status': 'success' if row_status == 'succeeded' else 'error' if row_status == 'failed' else row_status,
                          'error_code': result.get('error_code') or ('execution_timeout' if row_status == 'timed_out' else None),
                          'output': node_outputs.get(node_id or '__end__'),
                          'node_outputs': node_outputs, 'errors': result.get('error_dict') or {},
                          'execution_time': result.get('execution_time', 0), 'model_calls': model_calls}
                output.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
                output.flush()
                completed += 1
                failed += row_status != 'succeeded'
                report('running')
            finally:
                for task in approval_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*approval_tasks, return_exceptions=True)

    tasks = []
    terminal = None
    report('running')
    try:
        tasks = [asyncio.create_task(worker()) for _ in range(capacity)]
        await asyncio.gather(*tasks)
        report('completed_with_errors' if failed else 'completed')
        return 1 if failed else 0
    except asyncio.CancelledError:
        terminal = 'cancelled'
        raise
    except Exception:
        terminal = 'failed'
        raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.close()
        # Cancellation stops the consumer before the engine emits its terminal
        # event. Drain that local tail so a registered approval is also closed.
        if event_sink is not None:
            for invocation_id, execution in list(runtime.executions.items()):
                try:
                    index = indexes[invocation_id]
                    while execution.acknowledged < execution.seq:
                        response = await runtime.events(invocation_id, after=execution.acknowledged)
                        for event in response['events']:
                            events.write(json.dumps({'index': index, **event}, ensure_ascii=False, default=str) + '\n')
                            events.flush()
                            await event_sink(runtime, invocation_id, index, event)
                        runtime.acknowledge(invocation_id, response['events'][-1]['seq'])
                except Exception as exc:
                    status({'warning': 'approval_history_sync_failed', 'index': indexes[invocation_id],
                            'error_type': type(exc).__name__})
        if terminal:
            report(terminal)
