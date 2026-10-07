import { execFileSync } from 'node:child_process';
import { describe, expect, it } from 'vitest';
import type { Deployment } from '@/lib/api/deployments';
import { deploymentCodeExamples } from '../deployment-code-examples';

const inputs = { text: "it's a test", enabled: true };

describe.each(['api', 'webhook'] as const)('%s executable examples', (trigger_type) => {
  const dep = { slug: 'example', trigger_type } as Deployment;
  it.each(['sync', 'waiting', 'already_completed'])('Python handles %s', scenario => {
    const code = deploymentCodeExamples(dep, inputs).python;
    const setup = `
import sys, types, os, time, json
os.environ['FLOWORK_API_KEY'] = 'test-key'
os.environ['FLOWORK_WEBHOOK_SECRET'] = 'test-secret'
time.sleep = lambda _: None
scenario = ${JSON.stringify(scenario)}
queries = []
class Response:
    def __init__(self, status, payload, url):
        self.status_code, self.payload, self.url = status, payload, url
    def json(self): return self.payload
    def raise_for_status(self): pass
terminal = {'status': 'succeeded', 'outputs': {'answer': 42}}
def post(url, **kwargs):
    if scenario == 'sync': return Response(200, terminal, url)
    return Response(202, {'status': 'succeeded' if scenario == 'already_completed' else 'waiting_approval',
        'invocation_id': 'run', 'status_url': '/api/v1/deployments/example/runs/run', 'poll_after_seconds': 1}, url)
def get(url, **kwargs):
    queries.append((url, kwargs))
    if ${JSON.stringify(trigger_type)} == 'api':
        assert kwargs['headers']['Authorization'] == 'Bearer test-key'
    else:
        import hmac, hashlib
        from urllib.parse import urlsplit
        ts = kwargs['headers']['X-Vibecanvas-Timestamp']
        expected = hmac.new(b'test-secret', f'{ts}.GET {urlsplit(url).path}'.encode(), hashlib.sha256).hexdigest()
        assert kwargs['headers']['X-Vibecanvas-Signature'] == 'sha256=' + expected
    return Response(200, terminal, url)
sys.modules['requests'] = types.SimpleNamespace(post=post, get=get)
`;
    const output = execFileSync('python3', ['-'], { input: setup + '\n' + code + '\nassert len(queries) == (0 if scenario == "sync" else 1)\n', encoding: 'utf8' });
    expect(output).toContain('42');
  });
  it('cURL remains syntactically valid for quoted input', () => {
    execFileSync('bash', ['-n'], { input: deploymentCodeExamples(dep, inputs).curl });
  });
});
