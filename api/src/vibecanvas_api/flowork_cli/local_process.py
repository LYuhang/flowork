"""A local engine process that survives its CLI only after approval handoff."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from uuid import uuid4


def launch(args, endpoint, cli, root):
    run_id = uuid4().hex
    directory = root / run_id
    directory.mkdir(parents=True, mode=0o700)
    read_fd, write_fd = os.pipe()
    process = None
    detached = False
    previous_handlers = {}
    try:
        with (directory / 'worker.log').open('x') as log:
            process = subprocess.Popen(
                [sys.executable, '-m', __name__, str(write_fd)],
                stdin=subprocess.PIPE, stdout=log, stderr=log,
                pass_fds=(write_fd,), start_new_session=True,
            )
        os.close(write_fd)
        write_fd = None

        def cancel(signum, frame):
            process.send_signal(signal.SIGTERM)
            raise KeyboardInterrupt

        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, cancel)
        payload = {'args': {**vars(args), '_run_id': run_id}, 'endpoint': endpoint, 'root': str(root)}
        process.stdin.write(json.dumps(payload).encode())
        process.stdin.close()
        with os.fdopen(read_fd, encoding='utf-8') as source:
            read_fd = None
            for line in source:
                frame = json.loads(line)
                value = frame['value']
                if frame['type'] == 'result':
                    process.wait()
                    return cli.emit_result(value, exit_code=frame['exit_code'], state_field='execution_status')
                if value.get('status') == 'waiting_approval':
                    # The worker persisted this state and registered the approval
                    # before emitting it. Closing IPC does not close its runtime.
                    detached = True
                    return cli.emit_result({**value, 'async': True,
                        'status_command': f'flowork-cli workflow status --run-id {run_id}',
                        'result_command': f'flowork-cli workflow result --run-id {run_id}'},
                        state_field='execution_status')
                cli.emit_progress(value, stream=args.stream)
        code = process.wait()
        return cli.emit_result({'error': 'local_worker_exited_without_result', 'run_id': run_id,
            'worker_exit_code': code}, exit_code=1)
    except KeyboardInterrupt:
        return cli.emit_result({'run_id': run_id, 'status': 'cancelled'}, exit_code=130,
                               state_field='execution_status')
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)
        if process is not None and not detached and process.poll() is None:
            process.terminate()
            process.wait()


def worker(fd):
    from vibecanvas_api.flowork_cli import cli, local_command
    os.set_inheritable(fd, False)
    payload = json.load(sys.stdin)
    sys.stdin.close()
    local_command.RUN_ROOT = Path(payload['root'])
    channel = os.fdopen(fd, 'w', encoding='utf-8')

    def send(kind, value, exit_code=0):
        if channel.closed:
            return
        try:
            channel.write(json.dumps({'type': kind, 'value': value, 'exit_code': exit_code}) + '\n')
            channel.flush()
        except BrokenPipeError:
            # Approval handoff releases only the CLI connection.
            try:
                channel.close()
            except BrokenPipeError:
                pass

    def result(value, *, exit_code=0, state_field=None):
        send('result', value, exit_code)
        return exit_code

    cli.emit_progress = lambda value, **kwargs: send('progress', value)
    cli.emit_result = result
    try:
        return local_command.execute_in_process(argparse.Namespace(**payload['args']), payload['endpoint'], cli)
    finally:
        if not channel.closed:
            channel.close()


if __name__ == '__main__':
    raise SystemExit(worker(int(sys.argv[1])))
