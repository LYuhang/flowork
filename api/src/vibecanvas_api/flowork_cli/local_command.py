"""CLI-owned execution with local results, progress and durable terminal status."""
from __future__ import annotations
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import date, datetime, time as datetime_time
import json
import os
from pathlib import Path
import signal
import time
from uuid import uuid4

from vibecanvas_api.services.sandbox.local_activity import execution_activity


def execute(args, endpoint, cli):
    from vibecanvas_api.flowork_cli.local_workflow import run_rows
    run_id = uuid4().hex
    summary = {'run_id': run_id, 'pid': os.getpid(), 'started_at': time.time(), 'status': 'preparing'}
    state_path = None
    try:
        if args.overwrite and not args.output:
            raise ValueError('--overwrite requires --output')
        sources = [args.input_file] if args.input_file else []
        if args.action == 'run':
            rows = [cli._run_json(cli._read_run_file(args.input_file).decode('utf-8-sig') if args.input_file else args.inputs or '{}')]
        else:
            from vibecanvas_api.services.agent_resources.table_io import _text_to_rows, _xlsx_to_rows
            raw = cli._read_run_file(args.input_file)
            ext = Path(args.input_file).suffix.lstrip('.').lower()
            if ext in {'xlsx', 'xlsm'}:
                rows, _ = _xlsx_to_rows(raw, args.input_file, args.sheet)
            else:
                if args.sheet:raise ValueError('--sheet requires a workbook')
                table = _text_to_rows(raw.decode('utf-8-sig'), ext)
                if table is None:raise ValueError('Unsupported input format')
                rows, _ = table
            def cell(value):
                if isinstance(value, (date, datetime, datetime_time)):return value.isoformat()
                raise ValueError('Unsupported cell value')
            rows = json.loads(json.dumps(rows, allow_nan=False, default=cell))
        if not rows or any(not isinstance(row, dict) or any(not isinstance(k, str) for k in row) for row in rows):
            raise ValueError('Inputs must be non-empty JSON objects')
        preparation = {'workflow_id': args.workflow_id, 'major': args.major, 'run_id': run_id}
        if getattr(args, 'file', None):
            preparation['workflow'] = cli.read_workflow(args.file)
            sources.append(args.file)
        if getattr(args, 'node', None):preparation['node'] = args.node
        path = os.path.abspath(args.output) if args.output else f'/data/runs/{run_id}/results.jsonl'
        if args.output is None:os.makedirs(os.path.dirname(path), exist_ok=False)
        with ExitStack() as files:
            files.enter_context(execution_activity(run_id=run_id))
            output = files.enter_context(cli._open_run_output(path, overwrite=args.overwrite, sources=sources))
            directory = Path(path).parent / '.flowork-runs' / run_id
            directory.mkdir(parents=True, mode=0o700)
            state_path = directory / 'status.json'
            events = files.enter_context((directory / 'events.jsonl').open('x', encoding='utf-8'))
            summary.update(name=getattr(args, 'name', None) or (Path(args.input_file).name if args.input_file else 'workflow run'), path=path, status_path=str(state_path), events_path=str(directory / 'events.jsonl'), total=len(rows), completed=0, failed=0)
            def report(update):
                summary.update(update)
                temporary = state_path.with_suffix('.tmp')
                temporary.write_text(json.dumps(summary, ensure_ascii=False, allow_nan=False), encoding='utf-8')
                os.replace(temporary, state_path)
                cli.emit_progress(dict(summary), stream=args.stream)
            report({})
            prepared = cli.request(endpoint, preparation, operation='workflow.prepare')
            if 'error' in prepared:
                report({'status':'failed', 'error':prepared['error'], 'exit_code':1})
                return cli.emit_result({**prepared, **summary}, exit_code=1, state_field='execution_status')
            summary.update({k:prepared[k] for k in ('id','version','source')})
            context = prepared['context']
            async def run():
                loop = asyncio.get_running_loop()
                task = asyncio.current_task()
                for sig in (signal.SIGTERM, signal.SIGINT):loop.add_signal_handler(sig, task.cancel)
                approvals = None
                if prepared.get('approval_service'):
                    from vibecanvas_api.flowork_cli.local_approval_client import LocalApprovalClient
                    approvals = LocalApprovalClient(descriptor=prepared['approval_service'],
                        workflow=prepared['workflow'], rows=rows, events_path=directory / 'events.jsonl',
                        progress=lambda value: cli.emit_progress(value, stream=args.stream))
                try:
                    return await run_rows(workflow=prepared['workflow'], rows=rows,
                        concurrency=args.concurrency if args.action=='run-batch' else 1,
                        output=output, events=events, status=report,
                        context_factory=lambda index: {**deepcopy(context), 'run_dir':'/run', 'run_id':run_id},
                        node_id=getattr(args, 'node', None), run_id=run_id,
                        approval_handler=approvals.approve if approvals else None,
                        event_sink=approvals.event if approvals else None)
                finally:
                    if approvals is not None:
                        await approvals.close()
                    for sig in (signal.SIGTERM, signal.SIGINT):loop.remove_signal_handler(sig)
            code = asyncio.run(run())
            report({'exit_code':code, 'finished_at':time.time()})
            return cli.emit_result(dict(summary), exit_code=code, state_field='execution_status')
    except (asyncio.CancelledError, KeyboardInterrupt):
        summary.update(status='cancelled', error='execution_cancelled', exit_code=130)
    except Exception as exc:
        # Store exception type, not a provider body or a resource capability.
        summary.update(status='failed', error='local_execution_failed', error_type=type(exc).__name__, exit_code=1)
    summary['finished_at'] = time.time()
    if state_path:
        temporary = state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(summary, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, state_path)
    return cli.emit_result(summary, exit_code=summary['exit_code'], state_field='execution_status')
