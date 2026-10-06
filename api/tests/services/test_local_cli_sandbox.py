"""Real Bubblewrap smoke test for the generated CLI and detached execution."""
import json
import os
from pathlib import Path
import shutil
import sys

import pytest


@pytest.mark.skipif(os.environ.get('FLOWORK_TEST_LOCAL_CLI_SANDBOX') != '1', reason='explicit real sandbox check')
def test_generated_cli_background_run_survives_gateway_turn_end(tmp_path):
    from vibecanvas_api.services.sandbox.bubblewrap import BubblewrapProvider
    from vibecanvas_api.services.sandbox.gvisor import _workflow_python_binds, _workflow_python_env
    import vibecanvas_api
    import vibecanvas_engine
    api_root = str(Path(vibecanvas_api.__file__).resolve().parents[1])
    engine_root = str(Path(vibecanvas_engine.__file__).resolve().parents[1])
    roots = [api_root, engine_root]
    graph = json.loads((Path(engine_root).parent/'tests'/'example_workflow.json').read_text())
    graph['node_2']['node_config']['process_fn'] = (
        "import time\ndef process_fn(inputs):\n    time.sleep(2)\n"
        "    return {'repeated': inputs['text'], 'char_count': len(inputs['text'])}"
    )
    for name in ('work', 'data', 'run'):
        (tmp_path/name).mkdir()
    (tmp_path/'graph.json').write_text(json.dumps(graph))
    (tmp_path/'data'/'inputs.jsonl').write_text('{"text":"one","count":1}\n{"text":"two","count":1}\n')
    script = r'''
import asyncio,json,os,sys,signal,shlex
from pathlib import Path
for path in os.environ.get('VC_SANDBOX_PYTHON_PATHS','').split(os.pathsep):
    if path and path not in sys.path:sys.path.append(path)
from vibecanvas_api.services.agent_runtime.cli_gateway import CliGateway
from vibecanvas_api.services.sandbox.local_activity import active_executions
async def main():
    gateway=CliGateway()
    calls={}
    operations=[]
    async def control(operation,args):
        key=args['call_id']
        if operation=='cli.start':
            operations.append(args['operation'])
            assert args['operation']=='workflow.prepare'
            calls[key]={'workflow':json.loads(Path('/runs/graph.json').read_text()),
                'context':{},'id':'qa-local','version':'v1.sv0','source':'saved'}
            return {'started':True}
        if operation=='cli.poll':
            return {'sequence':1,'event':{'terminal':True,'result':calls[key]}}
        if operation=='cli.cancel':return {'cancelled':True}
        raise AssertionError(operation)
    env={**os.environ,**await gateway.activate(control)}
    command='setsid nohup '+shlex.quote(env['FLOWORK_CLI_BIN'])+' workflow run-batch --workflow-id qa-local --major v1 --input-file /data/inputs.jsonl --output /data/results.jsonl --concurrency 1 --stream >/data/command.log 2>&1 </dev/null & echo $!'
    shell=await asyncio.create_subprocess_exec('/bin/bash','-c',command,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,start_new_session=True)
    stdout,stderr=await shell.communicate()
    assert shell.returncode==0,stderr.decode()
    child=int(stdout.decode().strip())
    async def state(wanted):
        while True:
            paths=list(Path('/data/.flowork-runs').glob('*/status.json'))
            if paths:
                value=json.loads(paths[0].read_text())
                if value['status'] in wanted:return value
                if value['status'] in {'failed','completed_with_errors','cancelled'}:
                    raise AssertionError(value)
            await asyncio.sleep(.05)
    try:
        await asyncio.wait_for(state({'running'}),15)
        assert active_executions('/work')==1
        # Native command tools clean up the launching process group.
        try:os.killpg(shell.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        os.kill(child,0)
        await gateway.deactivate()
        result=await asyncio.wait_for(state({'completed'}),15)
        # The result event precedes interpreter exit/lock release briefly.
        for _ in range(100):
            if active_executions('/work')==0:break
            await asyncio.sleep(.05)
        assert active_executions('/work')==0
        rows=[json.loads(line) for line in Path('/data/results.jsonl').read_text().splitlines()]
        assert len(rows)==2 and all(row['status']=='success' for row in rows)
        assert operations==['workflow.prepare']
        print(json.dumps({'completed':result['completed'],'exit_code':result['exit_code'],'gateway_deactivated':True,'rows':len(rows)}),flush=True)
    finally:
        try:os.kill(child,signal.SIGTERM)
        except ProcessLookupError:pass
        await gateway.close()
asyncio.run(main())
'''
    (tmp_path/'check.py').write_text(script)
    env = _workflow_python_env()
    env['PYTHONPATH'] = os.pathsep.join(roots)
    provider = BubblewrapProvider(shutil.which('bwrap'))
    handle = provider.run_serve(runs_root=str(tmp_path), work_dir=str(tmp_path/'work'),
        ro_binds=[*_workflow_python_binds(), *roots], env=env, network='none',
        command=[sys.executable, '/runs/check.py'],
        extra_rw_binds=[('/data',str(tmp_path/'data')),('/run',str(tmp_path/'run'))])
    try:
        stdout,stderr = handle.proc.communicate(timeout=40)
        log = (tmp_path/'data'/'command.log').read_text() if (tmp_path/'data'/'command.log').exists() else ''
        assert handle.proc.returncode == 0, stderr + '\n' + log
        assert json.loads(stdout.strip().splitlines()[-1]) == {
            'completed': 2, 'exit_code': 0, 'gateway_deactivated': True, 'rows': 2}
    finally:
        provider.stop_serve(handle)
