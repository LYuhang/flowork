import type { Deployment } from '@/lib/api/deployments';
import { resolveApiUrl } from '@/lib/base-path';
import { withResultQueries } from './deployment-result-examples';

function shellQuote(value: string): string {
  return "'" + value.replaceAll("'", "'\"'\"'") + "'";
}

export type CodeLanguage = 'curl' | 'python' | 'javascript';

export function endpointFor(dep: Deployment): string {
  return dep.trigger_type === 'webhook'
    ? `/api/v1/deployments/${dep.slug}/webhook`
    : `/api/v1/deployments/${dep.slug}/invoke`;
}

function invocationExamples(
  dep: Deployment,
  exampleInputs: Record<string, unknown>,
): Record<CodeLanguage, string> {
  const endpointPath = endpointFor(dep);
  if (!endpointPath) {
    return { curl: '', python: '', javascript: '' };
  }
  const endpoint = resolveApiUrl(endpointPath);
  const payload = JSON.stringify(exampleInputs);
  if (dep.trigger_type === 'webhook') {
    return {
      curl: [
        `payload=${shellQuote(payload)}`,
        'timestamp="$(date +%s)"',
        'signature="$(printf \'%s\' "${timestamp}.${payload}" | openssl dgst -sha256 -hmac "${FLOWORK_WEBHOOK_SECRET}" | awk \'{print $2}\')"',
        '',
        `curl --request POST '${endpoint}' \\`,
        "  --header 'Content-Type: application/json' \\",
        '  --header "X-Vibecanvas-Timestamp: ${timestamp}" \\',
        '  --header "X-Vibecanvas-Signature: sha256=${signature}" \\',
        '  --data "${payload}"',
      ].join('\n'),
      python: [
        'import hashlib',
        'import hmac',
        'import json',
        'import os',
        'import time',
        '',
        'import requests',
        '',
        `url = ${JSON.stringify(endpoint)}`,
        `payload = json.dumps(json.loads(${JSON.stringify(payload)}), separators=(",", ":"))`,
        'timestamp = str(int(time.time()))',
        'secret = os.environ["FLOWORK_WEBHOOK_SECRET"].encode()',
        'signature = hmac.new(',
        '    secret, f"{timestamp}.{payload}".encode(), hashlib.sha256',
        ').hexdigest()',
        'response = requests.post(',
        '    url,',
        '    data=payload,',
        '    headers={',
        '        "Content-Type": "application/json",',
        '        "X-Vibecanvas-Timestamp": timestamp,',
        '        "X-Vibecanvas-Signature": f"sha256={signature}",',
        '    },',
        `    timeout=${Math.max(60, (dep.timeout_seconds ?? 60) + 10)},`,
        ')',
        'response.raise_for_status()',
        'print(response.json())',
      ].join('\n'),
      javascript: [
        "import { createHmac } from 'node:crypto';",
        '',
        `const url = ${JSON.stringify(endpoint)};`,
        `const payload = JSON.stringify(${payload});`,
        'const timestamp = Math.floor(Date.now() / 1000).toString();',
        "const secret = process.env.FLOWORK_WEBHOOK_SECRET;",
        "if (!secret) throw new Error('FLOWORK_WEBHOOK_SECRET is required');",
        "const signature = createHmac('sha256', secret)",
        "  .update(`${timestamp}.${payload}`)",
        "  .digest('hex');",
        'const response = await fetch(url, {',
        "  method: 'POST',",
        '  headers: {',
        "    'Content-Type': 'application/json',",
        "    'X-Vibecanvas-Timestamp': timestamp,",
        "    'X-Vibecanvas-Signature': `sha256=${signature}` ,",
        '  },',
        '  body: payload,',
        '});',
        'if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);',
        'console.log(await response.json());',
      ].join('\n'),
    };
  }

  return {
    curl: [
      `curl --request POST '${endpoint}' \\`,
      "  --header 'Content-Type: application/json' \\",
      '  --header "Authorization: Bearer ${FLOWORK_API_KEY}" \\',
      `  --data ${shellQuote(payload)}`,
    ].join('\n'),
    python: [
      'import os',
      'import json',
      'import requests',
      '',
      `response = requests.post(${JSON.stringify(endpoint)},`,
      '    headers={"Authorization": f"Bearer {os.environ[\'FLOWORK_API_KEY\']}"},',
      `    json=json.loads(${JSON.stringify(payload)}),`,
      `    timeout=${Math.max(60, (dep.timeout_seconds ?? 60) + 10)},`,
      ')',
      'response.raise_for_status()',
      'print(response.json())',
    ].join('\n'),
    javascript: [
      `const response = await fetch(${JSON.stringify(endpoint)}, {`,
      "  method: 'POST',",
      '  headers: {',
      "    'Content-Type': 'application/json',",
      "    Authorization: `Bearer ${process.env.FLOWORK_API_KEY}` ,",
      '  },',
      `  body: JSON.stringify(${payload}),`,
      '});',
      'if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);',
      'console.log(await response.json());',
    ].join('\n'),
  };
}

export function deploymentCodeExamples(dep: Deployment, inputs: Record<string, unknown>): Record<CodeLanguage, string> {
  return withResultQueries(invocationExamples(dep, inputs), dep.trigger_type === 'webhook');
}
