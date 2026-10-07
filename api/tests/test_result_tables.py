import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from vibecanvas_api.services import result_tables as tables
from vibecanvas_api.services.object_store import InMemoryObjectStore, FilesystemObjectStore
from vibecanvas_api.routes import tasks


@pytest.fixture
def store(monkeypatch):
    value = InMemoryObjectStore()
    monkeypatch.setattr(tables, 'get_task_result_store', lambda: value)
    monkeypatch.setattr(tables, '_cache', tables.ResultTableCache(max_bytes=8192, max_entries=2))
    return value


def write(store, key='rows', value=None):
    return store.put_bytes(key, b'\n'.join(json.dumps(row).encode() for row in (value or [{'index': 2, 'status': 'success'}, {'index': 1, 'status': 'error'}])))


def test_reuse_invalidation_deletion_and_immutable_pages(store, monkeypatch):
    uri = write(store)
    original = store.iter_bytes
    reads = []
    def read(*args):
        reads.append(args)
        return original(*args)
    monkeypatch.setattr(store, 'iter_bytes', read)
    scope = ('tenant', 'user', 'task')
    first = tables.query_result_table(scope, uri, tasks.ResultQuery())
    first['rows'][0]['status'] = 'changed'
    second = tables.query_result_table(scope, uri, tasks.ResultQuery(descending=True))
    assert len(reads) == 1 and second['rows'][1]['status'] == 'error'
    assert second['counts'] == {'success': 1, 'error': 1, 'cancelled': 0}
    write(store, value=[{'index': 3, 'status': 'cancelled'}])
    third = tables.query_result_table(scope, uri, tasks.ResultQuery())
    assert third['total'] == 1 and third['version'] != second['version']
    store.delete_bytes('rows')
    with pytest.raises(HTTPException) as exc:
        tables.query_result_table(scope, uri, tasks.ResultQuery())
    assert exc.value.status_code == 404


def test_cache_scope_lru_and_memory_bound(store):
    uri = write(store)
    for user in ('one', 'two', 'three'):
        tables.query_result_table(('tenant', user, 'task'), uri, tasks.ResultQuery())
    assert len(tables._cache.entries) == 2
    assert {key[1] for key in tables._cache.entries} == {'two', 'three'}
    assert tables._cache.bytes <= tables._cache.max_bytes
    tables._cache.max_bytes = 1
    write(store, value=[{'index': 3}])
    tables.query_result_table(('tenant', 'two', 'task'), uri, tasks.ResultQuery())
    assert all(key[1] != 'two' for key in tables._cache.entries)


def test_changed_during_read_is_not_cached(store, monkeypatch):
    uri = write(store)
    versions = iter(('old', 'new'))
    monkeypatch.setattr(store, 'revision', lambda _: next(versions))
    with pytest.raises(HTTPException) as exc:
        tables.query_result_table(('t', 'u', 'task'), uri, tasks.ResultQuery())
    assert exc.value.status_code == 409 and not tables._cache.entries


def test_encrypted_filesystem_revision_changes(tmp_path):
    store = FilesystemObjectStore(root=str(tmp_path / 'objects'), master_key=b'x' * 32)
    write(store)
    before = store.revision('rows')
    write(store, value=[{'index': 3}])
    assert store.revision('rows') != before
    store.delete_bytes('rows')
    with pytest.raises(KeyError):
        store.revision('rows')


@pytest.mark.asyncio
async def test_query_does_not_block_event_loop(monkeypatch):
    uri = 'memory://rows'
    task = SimpleNamespace(task_type='batch_exec', status='finished', result={'artifact_uris': {'jsonl': uri}})
    monkeypatch.setattr(tasks, '_authorize_task', AsyncMock())
    monkeypatch.setattr(tasks, 'TasksRepo', lambda _: SimpleNamespace(get=AsyncMock(return_value=task)))
    ready = threading.Event()
    def slow(*args):
        ready.set()
        time.sleep(0.15)
        return {'rows': []}
    monkeypatch.setattr(tables, 'query_result_table', slow)
    query = asyncio.create_task(tasks.query_results(uuid4(), tasks.ResultQuery(), None,
        SimpleNamespace(tenant_id=uuid4(), user_id=uuid4()), None, None))
    while not ready.is_set():
        await asyncio.sleep(0.001)
    await asyncio.sleep(0.02)
    assert not query.done()
    assert await query == {'rows': [], 'partial': False}


def test_concurrent_queries_load_once(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    uri = write(store)
    original = store.iter_bytes
    reads = []
    def read(key):
        reads.append(key)
        time.sleep(0.03)
        return original(key)
    monkeypatch.setattr(store, 'iter_bytes', read)
    with ThreadPoolExecutor(max_workers=4) as workers:
        pages = list(workers.map(lambda _: tables.query_result_table(('t', 'u', 'task'), uri, tasks.ResultQuery()), range(4)))
    assert reads == ['rows'] and all(page == pages[0] for page in pages)


def test_oversized_or_invalid_results_never_enter_cache(store, monkeypatch):
    from vibecanvas_api.services import batch_evaluation
    uri = write(store)
    monkeypatch.setattr(batch_evaluation, 'MAX_RESULT_BYTES', 10)
    with pytest.raises(HTTPException) as exc:
        tables.query_result_table(('t', 'u', 'task'), uri, tasks.ResultQuery())
    assert exc.value.status_code == 413 and not tables._cache.entries
    store.put_bytes('rows', b'not json')
    with pytest.raises(HTTPException) as exc:
        tables.query_result_table(('t', 'u', 'task'), uri, tasks.ResultQuery())
    assert exc.value.status_code == 422 and not tables._cache.entries


@pytest.mark.asyncio
async def test_authorization_precedes_any_cached_read(monkeypatch):
    from unittest.mock import Mock
    read = Mock()
    monkeypatch.setattr(tables, 'query_result_table', read)
    monkeypatch.setattr(tasks, '_authorize_task', AsyncMock(side_effect=HTTPException(404)))
    with pytest.raises(HTTPException):
        await tasks.query_results(uuid4(), tasks.ResultQuery(), None, None, None, None)
    read.assert_not_called()


def test_s3_revision_tracks_current_metadata_and_missing_key():
    from unittest.mock import Mock
    from botocore.exceptions import ClientError
    from vibecanvas_api.services.object_store import S3ObjectStore
    store = object.__new__(S3ObjectStore)
    store.bucket = 'qa'
    store.client = SimpleNamespace(head_object=Mock(return_value={
        'VersionId': 'v1', 'ETag': 'first', 'ContentLength': 10, 'LastModified': 'now'}))
    first = store.revision('rows')
    store.client.head_object.return_value['VersionId'] = 'v2'
    assert store.revision('rows') != first
    store.client.head_object.side_effect = ClientError({'Error': {'Code': '404'}}, 'HeadObject')
    with pytest.raises(KeyError):
        store.revision('rows')


def test_large_filesystem_result_pages_revision_and_cache_budget(tmp_path, monkeypatch):
    store = FilesystemObjectStore(root=str(tmp_path / 'large-results'), master_key=b'q' * 32)
    monkeypatch.setattr(tables, 'get_task_result_store', lambda: store)
    cache = tables.ResultTableCache()
    monkeypatch.setattr(tables, '_cache', cache)
    rows = [{'index': i, 'status': 'error' if i % 7 == 0 else 'success',
             'output': {'label': f'case-{i:05}', 'explanation': 'evidence ' * 30}}
            for i in range(20000)]
    uri = write(store, key='large', value=rows)
    query = tasks.ResultQuery(row_status='error', sort='index', descending=True, offset=10, limit=25)
    result = tables.query_result_table(('tenant-a', 'user-a', 'task-a'), uri, query)
    expected = sorted((r for r in rows if r['status'] == 'error'), key=lambda r: r['index'], reverse=True)[10:35]
    assert result['rows'] == expected and result['total'] == len(rows)
    assert result['filtered'] == len([r for r in rows if r['status'] == 'error'])
    assert cache.bytes <= cache.max_bytes and len(cache.entries) <= cache.max_entries
    # Replace the same actual encrypted file; a cached page must not survive.
    write(store, key='large', value=[{'index': 99, 'status': 'cancelled', 'output': {'label': 'replacement'}}])
    replacement = tables.query_result_table(('tenant-a', 'user-a', 'task-a'), uri, tasks.ResultQuery())
    assert replacement['version'] != result['version']
    assert replacement['total'] == 1 and replacement['rows'][0]['status'] == 'cancelled'
    # Distinct task file and scope must retain their own output.
    other = write(store, key='other', value=[{'index': 1, 'status': 'success', 'output': {'label': 'other-task'}}])
    page = tables.query_result_table(('tenant-b', 'user-b', 'task-b'), other, tasks.ResultQuery())
    assert page['rows'][0]['output']['label'] == 'other-task'
    assert tables.query_result_table(('tenant-a', 'user-a', 'task-a'), uri, tasks.ResultQuery()) == replacement


@pytest.mark.asyncio
async def test_large_concurrent_queries_keep_event_loop_responsive(tmp_path, monkeypatch):
    store = FilesystemObjectStore(root=str(tmp_path / 'concurrent-results'), master_key=b'r' * 32)
    monkeypatch.setattr(tables, 'get_task_result_store', lambda: store)
    monkeypatch.setattr(tables, '_cache', tables.ResultTableCache())
    rows = [{'index': i, 'status': 'success', 'output': {'text': 'evidence ' * 30}}
            for i in range(20000)]
    uri = write(store, key='large', value=rows)
    task = SimpleNamespace(task_type='batch_exec', status='finished',
                           result={'artifact_uris': {'jsonl': uri}})
    monkeypatch.setattr(tasks, '_authorize_task', AsyncMock())
    monkeypatch.setattr(tasks, 'TasksRepo', lambda _: SimpleNamespace(get=AsyncMock(return_value=task)))
    identifier = uuid4()
    auth = SimpleNamespace(tenant_id=uuid4(), user_id=uuid4())
    pending = [asyncio.create_task(tasks.query_results(identifier,
        tasks.ResultQuery(search='evidence', descending=True, offset=i * 10, limit=10),
        None, auth, None, None)) for i in range(4)]
    gaps = []
    start = time.perf_counter()
    while not all(query.done() for query in pending):
        tick = time.perf_counter()
        await asyncio.sleep(0.005)
        if any(not query.done() for query in pending):
            gaps.append(time.perf_counter() - tick)
    results = await asyncio.gather(*pending)
    assert gaps, 'Queries monopolized the event loop until all completed'
    for i, result in enumerate(results):
        assert result['total'] == result['filtered'] == 20000
        assert result['rows'] == list(reversed(rows))[i * 10:i * 10 + 10]
    assert tables._cache.bytes <= tables._cache.max_bytes
    print(json.dumps({'rows': 20000, 'queries': 4, 'seconds': time.perf_counter() - start,
                      'responsive_ticks': len(gaps), 'max_tick_gap_seconds': max(gaps)}))
