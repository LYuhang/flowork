import io

import pytest

from vibecanvas_api.security.object_cipher import LocalObjectCipher


class ShortReads(io.BytesIO):
    def read(self, size=-1):
        assert 0 <= size <= 256 * 1024
        return super().read(min(size, 777))


@pytest.mark.parametrize('size', [0, 1, 256 * 1024, 256 * 1024 + 17])
def test_stream_roundtrip_with_short_reads(size):
    data = (b'hello-world' * ((size // 11) + 1))[:size]
    cipher = LocalObjectCipher(b'k' * 32)
    output = io.BytesIO()
    cipher.write_stream(output, key='test', source=ShortReads(data), size=size)
    output.seek(0)
    assert cipher.read(output, key='test') == data


@pytest.mark.parametrize('data,size', [(b'abc', 4), (b'abcd', 3), (b'', -1)])
def test_stream_rejects_incorrect_length(data, size):
    with pytest.raises(ValueError):
        LocalObjectCipher(b'k' * 32).write_stream(
            io.BytesIO(), key='test', source=ShortReads(data), size=size)


def test_existing_bytes_writer_remains_readable():
    cipher = LocalObjectCipher(b'k' * 32)
    output = io.BytesIO()
    cipher.write(output, key='test', plaintext=b'hello')
    output.seek(0)
    assert cipher.read(output, key='test') == b'hello'


def test_stream_store_failure_preserves_previous_object(tmp_path):
    from vibecanvas_api.services.object_store import FilesystemObjectStore
    store = FilesystemObjectStore(root=str(tmp_path / 'objects'), master_key=b'k' * 32)
    store.put_bytes('test/item', b'old value')
    with pytest.raises(ValueError):
        store.persist_materialized_stream('test/item', ShortReads(b'short'), size=20)
    assert store.fetch_bytes('test/item') == b'old value'
    assert not list((tmp_path / 'objects').rglob('.vcobj-*'))
    store.persist_materialized_stream('test/item', ShortReads(b'new value'), size=9)
    assert store.fetch_bytes('test/item') == b'new value'
