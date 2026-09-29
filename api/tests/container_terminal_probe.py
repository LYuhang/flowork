import base64
import time
import uuid
from vibecanvas_api.routes import deployment_terminal, deployments, deployment_invoke
from vibecanvas_api.services.deployment_completion import complete_before_cancelling
from vibecanvas_api.services.sandbox.terminal import terminal_operation
from vibecanvas_api.services.agent_runtime.cli_gateway import platform_guidance
assert platform_guidance().strip(), 'Packaged agent instructions are missing'
identifier = str(uuid.uuid4())
def op(action, **fields):
    result = terminal_operation({'terminal_id': identifier, 'action': action, **fields})
    assert result.get('ok'), result
    return result
try:
    op('open', columns=96, rows=28)
    op('write', data=base64.b64encode(b'stty size\r').decode())
    output = ''
    for _ in range(50):
        output += base64.b64decode(op('read')['data']).decode(errors='replace')
        if '28 96' in output:
            break
        time.sleep(.05)
    assert '28 96' in output, output
    print('Packaged API routes, cancellation completion helper and real Bash PTY: OK')
finally:
    op('close')
