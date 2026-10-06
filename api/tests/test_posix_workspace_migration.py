import tracemalloc

import pytest

from vibecanvas_api.security.posix_workspace_migration import migrate_file


def test_stream_migration_verifies_and_refuses_conflicting_destination(tmp_path):
    kwargs = dict(root=tmp_path, relative='project/data/file', expected_size=6)
    result = migrate_file(iter([b'abc', b'def']), **kwargs)
    assert result.size_bytes == 6 and not result.already_present
    assert migrate_file(iter([b'abcdef']), **kwargs).already_present
    with pytest.raises(ValueError, match='conflict'):
        migrate_file(iter([b'newnew']), **kwargs)
    assert (tmp_path / kwargs['relative']).read_bytes() == b'abcdef'
    assert not list(tmp_path.rglob('.migration-*'))


def test_failed_or_truncated_source_is_never_published(tmp_path):
    def corrupt():
        yield b'partial'
        raise ValueError('source authentication failed')
    with pytest.raises(ValueError, match='authentication'):
        migrate_file(corrupt(), root=tmp_path, relative='file')
    with pytest.raises(ValueError, match='size'):
        migrate_file([b'short'], root=tmp_path, relative='file', expected_size=10)
    assert list(tmp_path.iterdir()) == []


def test_large_migration_memory_is_bounded(tmp_path):
    chunk = b'x' * (1024 * 1024)
    tracemalloc.start()
    try:
        result = migrate_file((chunk for _ in range(32)), root=tmp_path, relative='large')
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.size_bytes == 32 * 1024 * 1024
    assert peak < 5 * 1024 * 1024


@pytest.mark.parametrize('relative', ['../escape', '/absolute', 'nested/../escape'])
def test_migration_rejects_unsafe_paths(tmp_path, relative):
    with pytest.raises(ValueError):
        migrate_file([b'value'], root=tmp_path, relative=relative)
