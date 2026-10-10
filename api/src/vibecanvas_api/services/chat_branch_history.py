"""Prepare authorized source history for a branch without mutating the source."""
from __future__ import annotations

from copy import deepcopy
import json


def _freeze_artifact(envelope: dict) -> None:
    """Mark canonical inline interactive definitions as immutable snapshots."""
    if envelope.get('kind') == 'interactive_artifact':
        envelope['read_only'] = True
    payload = envelope.get('payload')
    if isinstance(payload, dict):
        definition = payload.get('artifact')
        if isinstance(definition, dict) and definition.get('kind') == 'interactive_artifact':
            definition['read_only'] = True
        # Approval hints belong to the original execution, never the branch.
        if 'pending_approval' in payload:
            payload['pending_approval'] = False
    meta = envelope.get('meta')
    if isinstance(meta, dict) and 'pending_approval' in meta:
        meta['pending_approval'] = False


def branch_message_snapshot(
    row: dict, *, source_chat_id: str, tool_projections: dict[str, dict],
    artifact_definitions: dict[str, dict] | None = None,
) -> dict:
    """Snapshot one message after the caller authorizes its source and boundary.

    tool_projections contains authoritative HITL envelopes from the same source
    Chat. Attachments stay references to the shared Project's current files;
    this function neither copies files nor claims those files still exist.
    """
    snapshot = deepcopy(row)
    snapshot['meta'] = {
        **snapshot.get('meta', {}),
        'branch_source': {
            'chat_id': source_chat_id, 'turn_id': row.get('turn_id'),
            'message_id': row['message_id'],
        },
    }
    content = snapshot['content']
    targets = [content, *(content.get('tool_calls') or [])]
    for target in targets:
        tool_id = target.get('tool_call_id') or target.get('id')
        if tool_id in tool_projections:
            target['artifact'] = deepcopy(tool_projections[tool_id])
        artifact = target.get('artifact')
        if isinstance(artifact, dict):
            _freeze_artifact(artifact)
        payload = artifact.get('payload') if isinstance(artifact, dict) else None
        payload = payload if isinstance(payload, dict) else {}
        definition = payload.get('artifact') or payload.get('artifact_preview')
        if definition is None and isinstance(target.get('result'), str):
            try:
                result = json.loads(target['result'])
            except ValueError:
                result = None
            if isinstance(result, dict) and isinstance(result.get('output'), dict):
                definition = result['output'].get('data')
        if isinstance(definition, dict) and definition.get('artifact_id'):
            saved = (artifact_definitions or {}).get(definition['artifact_id'])
            if saved is not None:
                definition = deepcopy(saved)
            elif definition.get('kind') != 'interactive_artifact':
                raise LookupError('branch_artifact_unavailable')
        if isinstance(definition, dict) and definition.get('kind') == 'interactive_artifact':
            definition = deepcopy(definition)
            definition['read_only'] = True
            if not isinstance(artifact, dict):
                artifact = {}
                target['artifact'] = artifact
            artifact['payload'] = {**payload, 'artifact': definition}

    return snapshot
