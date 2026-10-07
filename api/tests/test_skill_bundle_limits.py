"""Bound archive resources before and during Skill extraction."""
import io
import zipfile
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from vibecanvas_api.config import config
from vibecanvas_api.services.skill_bundle import unpack_skill_zip
from vibecanvas_api.services.skill_loader import SkillParseError

SKILL = b'---\nname: example\ndescription: example skill\n---\nInstructions\n'


def bundle(entries):
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return result.getvalue()


def test_normal_bundle():
    metadata, files = unpack_skill_zip(bundle([('SKILL.md', SKILL), ('references/a.txt', b'hello')]))
    assert metadata['name'] == 'example'
    assert files[-1] == ('references/a.txt', None, b'hello')


@pytest.mark.parametrize('case', ['file', 'total', 'count', 'duplicate', 'path'])
def test_preflight_rejects_before_opening_entries(monkeypatch, case):
    entries = [('SKILL.md', SKILL), ('large.txt', b'x' * 65536)]
    if case == 'file':
        monkeypatch.setattr(config.skills, 'max_file_bytes', 1024)
    elif case == 'total':
        monkeypatch.setattr(config.skills, 'max_bundle_bytes', 1024)
    elif case == 'count':
        monkeypatch.setattr(config.skills, 'max_files', 1)
    elif case == 'duplicate':
        entries = [('SKILL.md', SKILL), ('./SKILL.md', SKILL)]
    else:
        entries = [('SKILL.md', SKILL), ('../bad', b'x')]
    data = bundle(entries)
    def forbidden(*args, **kwargs):
        pytest.fail('entry decompressed before preflight rejected bundle')
    monkeypatch.setattr(zipfile.ZipFile, 'open', forbidden)
    with pytest.raises(SkillParseError):
        unpack_skill_zip(data)


def test_actual_read_is_bounded(monkeypatch):
    data = bundle([('SKILL.md', SKILL)])
    monkeypatch.setattr(config.skills, 'max_file_bytes', 100)
    sizes = []
    class InflatedEntry(io.BytesIO):
        def read(self, size=-1):
            sizes.append(size)
            return super().read(size)
    monkeypatch.setattr(zipfile.ZipFile, 'open', lambda *a, **kw: InflatedEntry(b'x' * 10000))
    with pytest.raises(SkillParseError, match='extraction limit'):
        unpack_skill_zip(data)
    assert sizes == [101]


def test_invalid_archive():
    with pytest.raises(SkillParseError, match='ZIP archive'):
        unpack_skill_zip(b'not a zip')


@pytest.mark.asyncio
async def test_upload_read_is_bounded_before_scan(monkeypatch):
    from vibecanvas_api.routes import skills
    monkeypatch.setattr(config.skills, 'max_bundle_bytes', 100)
    upload = AsyncMock()
    upload.read.return_value = b'x' * 101
    scanner = AsyncMock()
    monkeypatch.setattr(skills, 'require_clean_upload', scanner)
    with pytest.raises(HTTPException) as error:
        await skills._read_custom_bundle(upload)
    assert error.value.status_code == 413
    upload.read.assert_awaited_once_with(101)
    scanner.assert_not_awaited()


def test_encrypted_flag_is_rejected_before_decompression(monkeypatch):
    import struct
    data = bytearray(bundle([('SKILL.md', SKILL)]))
    central = data.index(b'PK\x01\x02')
    struct.pack_into('<H', data, central + 8, 1)
    def forbidden(*args, **kwargs):
        pytest.fail('encrypted entry must not be opened')
    monkeypatch.setattr(zipfile.ZipFile, 'open', forbidden)
    with pytest.raises(SkillParseError, match='encrypted'):
        unpack_skill_zip(bytes(data))


def test_corrupt_entry_crc_becomes_a_validation_error():
    import struct
    data = bytearray(bundle([('SKILL.md', SKILL)]))
    central = data.index(b'PK\x01\x02')
    crc = struct.unpack_from('<I', data, central + 16)[0]
    struct.pack_into('<I', data, central + 16, crc ^ 1)
    with pytest.raises(SkillParseError, match='ZIP archive'):
        unpack_skill_zip(bytes(data))


def test_truncated_archive_becomes_a_validation_error():
    data = bundle([('SKILL.md', SKILL)])
    with pytest.raises(SkillParseError, match='ZIP archive'):
        unpack_skill_zip(data[:-22])
