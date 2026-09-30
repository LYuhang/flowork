"""Runtime-neutral context serialization; deliberately free of host imports."""
from __future__ import annotations

import json


def context_text(attachment: dict) -> str:
    if attachment.get('schema_version') != 1:
        if attachment.get('type') in {'file', 'image', 'video'} and attachment.get('path'):
            return json.dumps({
                'kind': 'file', 'name': attachment.get('name'),
                'path': attachment['path'], 'content_type': attachment.get('content_type'),
            }, ensure_ascii=False)
        return ''
    if attachment.get('type') == 'quote':
        return json.dumps({
            'label': attachment['label'], 'source': attachment['source'],
            'selector': attachment.get('selector'), 'quoted_text': attachment['snapshot']['text'],
        }, ensure_ascii=False)
    resolved = attachment.get('resolved_text')
    if isinstance(resolved, str):
        return resolved
    # Durable history contains the host-created resource snapshot. It is user
    # reference material, just like quotes, never a tool/system instruction.
    return json.dumps(attachment, ensure_ascii=False)
