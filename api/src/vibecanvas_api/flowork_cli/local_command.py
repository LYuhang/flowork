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

from vibecanvas_api.services.sandbox.local_activity import execution_activity, execution_alive
from vibecanvas_api.flowork_cli.local_output import TERMINAL, describe

RUN_ROOT = Path('/data/runs')


def query(args, cli):
    """Read this sandbox's persisted execution evidence without submitting work."""
    try:
        return _query(args, cli)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return cli.emit_result({'run_id': args.run_id, 'error': 'execution_evidence_unavailable',
            'error_type': type(exc).__name__,
            'message': 'The saved execution status or result could not be read.',
            'hint': 'Verify this sandbox and the saved result files. Do not submit a replacement execution automatically.'},
            exit_code=1)


def _query(args, cli):
    from uuid import UUID
    try:
        run_id = UUID(args.run_id).hex
    except ValueError:
        return cli.emit_result({'error': 'invalid_run_id'}, exit_code=2)
    try:
        summary = json.loads((RUN_ROOT / run_id / 'status.json').read_text())
    except FileNotFoundError:
        return cli.emit_result({'error': 'run_not_found', 'run_id': run_id,
            'hint': 'Query in the original workspace and verify the run ID. Absence here does not prove execution failure; do not automatically rerun.'}, exit_code=1)
    if summary['status'] not in TERMINAL:
        alive = execution_alive(run_id)
        if alive is not True:
            # Completion may have committed between the first read and lock check.
            summary = json.loads((RUN_ROOT / run_id / 'status.json').read_text())
            if summary['status'] not in TERMINAL:
                summary.update(last_known_status=summary['status'],
                    status='interrupted' if alive is False else 'unknown', approvals=[],
                    error='execution_process_exited' if alive is False else 'execution_runtime_unavailable')
    if args.action == 'status':
        return cli.emit_result(describe(summary), state_field='execution_status')
    if args.offset < 0 or not 1 <= args.limit <= 1000 or (args.index is not None and args.index < 0):
        return cli.emit_result({'error': 'invalid_pagination'}, exit_code=2)
    records = []
    matched = 0
    with Path(summary['path']).open(encoding='utf-8') as source:
        for line in source:
            # A concurrently appended final line is not committed until newline.
            if not line.endswith('\n'):
                break
            record = json.loads(line)
            if args.index is not None and record['index'] != args.index:
                continue
            if matched >= args.offset:
                records.append(record)
            matched += 1
            if len(records) > args.limit:
                break
    more = len(records) > args.limit
    terminal = summary['status'] in TERMINAL
    complete = terminal and summary.get('total') is not None and summary.get('completed') == summary['total']
    next_offset = args.offset + args.limit if more else None
    next_command = None
    if more:
        next_command = (f'flowork-cli workflow result --run-id {run_id}'
                        f' --offset {next_offset} --limit {args.limit}')
        if args.index is not None:
            next_command += f' --index {args.index}'
    value = {**describe(summary), 'results': records[:args.limit],
        'results_complete': complete,
        'pagination': {'offset': args.offset, 'limit': args.limit, 'index': args.index,
                       'next_offset': next_offset, 'next_command': next_command},
        'partial': not complete,
        'result_available': bool(records),
        'next_offset': next_offset}
    if terminal:
        value['next_action'] = ({'type': 'read_next_page',
            'message': 'Read the next result page before drawing conclusions about the whole execution.',
            'command': next_command} if more else {'type': 'analyze_results',
            'message': 'Analyze the returned sample outcomes and errors. End of available pages is not proof that all samples succeeded or finished. Do not automatically rerun.',
            'command': None})
    return cli.emit_result(value, state_field='execution_status')


def execute(args, endpoint, cli):
    from vibecanvas_api.flowork_cli.local_process import launch
    return launch(args, endpoint, cli, RUN_ROOT)


def execute_in_process(args, endpoint, cli):
    run_id = getattr(args, '_run_id', None) or uuid4().hex
    with execution_activity(run_id=run_id):
        return _execute(args, endpoint, cli, run_id)


def _execute(args, endpoint, cli, run_id):
    from vibecanvas_api.flowork_cli.local_workflow import run_rows
    summary = {'run_id': run_id, 'pid': os.getpid(), 'started_at': time.time(), 'status': 'preparing',
               'workflow_id': args.workflow_id, 'mode': 'batch' if args.action == 'run-batch' else 'single',
               'async': False, 'approvals': [],
               'status_command': f'flowork-cli workflow status --run-id {run_id}',
               'result_command': f'flowork-cli workflow result --run-id {run_id}'}
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
        directory = RUN_ROOT / run_id
        directory.mkdir(parents=True, mode=0o700, exist_ok=bool(getattr(args, '_run_id', None)))
        path = os.path.abspath(args.output) if args.output else str(directory / 'results.jsonl')
        with ExitStack() as files:
            output = files.enter_context(cli._open_run_output(path, overwrite=args.overwrite, sources=sources))
            state_path = directory / 'status.json'
            events = files.enter_context((directory / 'events.jsonl').open('x', encoding='utf-8'))
            summary.update(name=getattr(args, 'name', None) or (Path(args.input_file).name if args.input_file else 'workflow run'), path=path, status_path=str(state_path), events_path=str(directory / 'events.jsonl'), total=len(rows), completed=0, failed=0)
            def report(update):
                summary.update(update)
                summary['updated_at'] = time.time()
                if update.get('status') == 'waiting_approval':
                    summary['async'] = True
                summary.update(describe(summary))
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
    summary.update(describe(summary))
    if state_path:
        temporary = state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(summary, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, state_path)
    return cli.emit_result(summary, exit_code=summary['exit_code'], state_field='execution_status')
