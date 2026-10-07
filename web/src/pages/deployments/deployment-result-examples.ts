/** Extend invocation examples with the public async receipt/query contract. */
export function withResultQueries(
  examples: Record<'curl' | 'python' | 'javascript', string>,
  webhook: boolean,
): Record<'curl' | 'python' | 'javascript', string> {
  const curlHeaders = webhook ? [
    'timestamp="$(date +%s)"',
    '# RESULT_PATH is the exact status_url from the 202 response, including any deployment prefix.',
    'signature="$(printf \'%s\' "${timestamp}.GET ${RESULT_PATH}" | openssl dgst -sha256 -hmac "${FLOWORK_WEBHOOK_SECRET}" | awk \'{print $2}\')"',
    'curl --request GET "${FLOWORK_ORIGIN}${RESULT_PATH}" \\',
    '  --header "X-Vibecanvas-Timestamp: ${timestamp}" \\',
    '  --header "X-Vibecanvas-Signature: sha256=${signature}"',
  ] : [
    '# RESULT_URL is the absolute URL resolved from the 202 Location/status_url.',
    'curl --request GET "${RESULT_URL}" \\',
    '  --header "Authorization: Bearer ${FLOWORK_API_KEY}"',
  ];
  const pythonHeaders = webhook ? [
    '        timestamp = str(int(time.time()))',
    '        signed_path = urlsplit(result_url).path',
    '        signature = hmac.new(secret, f"{timestamp}.GET {signed_path}".encode(), hashlib.sha256).hexdigest()',
    '        headers = {"X-Vibecanvas-Timestamp": timestamp, "X-Vibecanvas-Signature": f"sha256={signature}"}',
  ] : ['        headers = {"Authorization": f"Bearer {os.environ[\'FLOWORK_API_KEY\']}"}'];
  const jsHeaders = webhook ? [
    '    const timestamp = Math.floor(Date.now() / 1000).toString();',
    '    const signature = createHmac("sha256", secret).update(`${timestamp}.GET ${resultUrl.pathname}`).digest("hex");',
    '    const headers = {"X-Vibecanvas-Timestamp": timestamp, "X-Vibecanvas-Signature": `sha256=${signature}`};',
  ] : ['    const headers = {Authorization: `Bearer ${process.env.FLOWORK_API_KEY}`};'];
  return {
    curl: examples.curl.replace('curl --request POST', 'curl --include --request POST') + '\n\n' + [
      '# HTTP 200: read outputs. HTTP 202: save invocation_id and status_url; do not submit again.',
      '# Query after poll_after_seconds (default 3). Stop on succeeded/failed/timed_out/cancelled.',
      ...curlHeaders,
    ].join('\n'),
    python: examples.python.replace('print(response.json())', '') + '\n' + [
      'from urllib.parse import urljoin, urlsplit',
      'import time',
      '',
      'result = response.json()',
      'if response.status_code == 202:',
      '    print("Accepted:", result["invocation_id"])',
      '    result_url = urljoin(response.url, result["status_url"])',
      '    interval = max(1, result.get("poll_after_seconds", 3))',
      '    deadline = time.monotonic() + float(os.getenv("CLIENT_WAIT_SECONDS", "600"))',
      '    while True:',
      '        if time.monotonic() >= deadline:',
      '            raise TimeoutError(f"Stopped waiting locally; execution continues. Query {result_url} later.")',
      '        time.sleep(interval)',
      ...pythonHeaders,
      '        status_response = requests.get(result_url, headers=headers, timeout=30)',
      '        status_response.raise_for_status()',
      '        result = status_response.json()',
      '        print("Status:", result["status"])',
      '        if result["status"] in {"succeeded", "failed", "timed_out", "cancelled"}:',
      '            break',
      'print(result)',
      'if result["status"] != "succeeded":',
      '    raise RuntimeError(result.get("error_code", result["status"]))',
      'print("Outputs:", result["outputs"])',
    ].join('\n'),
    javascript: examples.javascript.replace('console.log(await response.json());', '') + '\n' + [
      'let result = await response.json();',
      'if (response.status === 202) {',
      '  console.log("Accepted:", result.invocation_id);',
      '  const resultUrl = new URL(result.status_url, response.url);',
      '  const interval = Math.max(1, result.poll_after_seconds ?? 3) * 1000;',
      '  const deadline = Date.now() + Number(process.env.CLIENT_WAIT_SECONDS ?? 600) * 1000;',
      '  while (true) {',
      '    if (Date.now() >= deadline) throw new Error(`Stopped waiting locally; query ${resultUrl} later. Execution continues.`);',
      '    await new Promise(resolve => setTimeout(resolve, interval));',
      ...jsHeaders,
      '    const statusResponse = await fetch(resultUrl, {headers, signal: AbortSignal.timeout(30000)});',
      '    if (!statusResponse.ok) throw new Error(`${statusResponse.status} ${await statusResponse.text()}`);',
      '    result = await statusResponse.json();',
      '    console.log("Status:", result.status);',
      '    if (["succeeded", "failed", "timed_out", "cancelled"].includes(result.status)) break;',
      '  }',
      '}',
      'if (result.status !== "succeeded") throw new Error(result.error_code ?? result.status);',
      'console.log("Outputs:", result.outputs);',
    ].join('\n'),
  };
}
