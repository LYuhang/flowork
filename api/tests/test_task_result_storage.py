from pathlib import Path

from vibecanvas_api.services import object_store as module


def test_posix_task_results_are_encrypted_and_shared_across_readers(tmp_path, monkeypatch):
    monkeypatch.setattr(module.config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(module.config, 'workspace_storage_root', str(tmp_path))
    monkeypatch.setattr(module, 'local_master_key_from_config', lambda **kwargs: b'k' * 32)
    store = module.get_task_result_store()
    payload = b'{"metric": 0.75, "private_result": "result data"}'
    uri = store.put_bytes('tasks/task-id/summary.json', payload, content_type='application/json')
    key = module.uri_to_key(uri)
    reader = module.FilesystemObjectStore(root=str(tmp_path / 'task-results-v1'),
        materialized_root=str(tmp_path / 'reader-materialized'), master_key=b'k' * 32)
    assert reader.fetch_bytes(key) == payload
    assert b''.join(reader.iter_bytes(key, start=2, end=6)) == payload[2:7]
    files = [p for p in (tmp_path / 'task-results-v1').rglob('*') if p.is_file()]
    assert files
    assert all(b'private_result' not in p.read_bytes() for p in files)
    assert not (tmp_path / 'workspaces-v1').exists()


def test_object_backend_uses_selected_object_store(monkeypatch):
    monkeypatch.setattr(module.config, 'workspace_storage_backend', 'object_store')
    expected = object()
    monkeypatch.setattr(module, 'get_object_store', lambda: expected)
    assert module.get_task_result_store() is expected


def test_migrate_task_result_preserves_uri_ciphertext_and_business_output(tmp_path):
    from vibecanvas_api.security.posix_workspace_migration import migrate_encrypted_task_result
    source = module.FilesystemObjectStore(root=str(tmp_path / 'old'),
        materialized_root=str(tmp_path / 'old-materialized'), master_key=b'k' * 32)
    target = module.FilesystemObjectStore(root=str(tmp_path / 'new'),
        materialized_root=str(tmp_path / 'new-materialized'), master_key=b'k' * 32)
    payload = b'{"output":{"score":0.8}}\n'
    uri = source.put_bytes('tasks/task-id/results.jsonl', payload)
    key = module.uri_to_key(uri)
    result = migrate_encrypted_task_result(source, target, key)
    assert not result.already_present
    assert target.fetch_bytes(module.uri_to_key(uri)) == payload
    assert (tmp_path / 'old' / key).read_bytes() == (tmp_path / 'new' / key).read_bytes()
    assert migrate_encrypted_task_result(source, target, key).already_present
