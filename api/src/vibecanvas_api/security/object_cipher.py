"""Chunked authenticated container for the local durable Object Store.

The filesystem backend cannot rely on cloud storage encryption.  Every object
therefore receives a random DEK, wrapped by the host-only local KMS key.  Data
is encrypted in independently authenticated chunks so HTTP Range/Preview reads
decrypt only the touched chunks.  There is deliberately no plaintext decoder:
old stores must be transformed by the offline migration command before a new
runtime starts.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import io
import json
import os
import struct
from typing import BinaryIO, Iterator

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from vibecanvas_api.security.crypto_core import (
    AES_KEY_BYTES,
    GCM_NONCE_BYTES,
    GCM_TAG_BYTES,
    SecretIntegrityError,
    wipe_bytes,
)


MAGIC = b"VCOBJ2\x00\x00"
_PREFIX = struct.Struct(">8sI")
_MAX_HEADER_BYTES = 16 * 1024
_TAG_BYTES = GCM_TAG_BYTES


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: object) -> bytes:
    if not isinstance(value, str):
        raise SecretIntegrityError("object container header is invalid")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise SecretIntegrityError("object container header is invalid") from exc


def is_encrypted_object_prefix(value: bytes) -> bool:
    return bytes(value[: len(MAGIC)]) == MAGIC


@dataclass(frozen=True, slots=True)
class ObjectHeader:
    plaintext_size: int
    chunk_size: int
    nonce_prefix: bytes
    wrapped_dek: bytes
    wrap_nonce: bytes
    key_version: str
    data_offset: int

    @property
    def chunk_count(self) -> int:
        if self.plaintext_size == 0:
            return 0
        return (self.plaintext_size + self.chunk_size - 1) // self.chunk_size


class LocalObjectCipher:
    """Fast local envelope encryption with bounded random-access overhead."""

    def __init__(self, master_key: bytes, *, chunk_size: int = 256 * 1024):
        if len(master_key) != AES_KEY_BYTES:
            raise ValueError("Object Store master key must be 32 bytes")
        if not 64 * 1024 <= int(chunk_size) <= 4 * 1024 * 1024:
            raise ValueError("Object Store encryption chunk size is out of range")
        self._master_key = bytes(master_key)
        self.chunk_size = int(chunk_size)
        self.key_version = hashlib.sha256(master_key).hexdigest()[:16]

    @staticmethod
    def _wrap_aad(key: str, key_version: str) -> bytes:
        return f"vibecanvas:object:v2\0{key}\0{key_version}".encode("utf-8")

    @staticmethod
    def _chunk_aad(
        key: str,
        *,
        index: int,
        plaintext_size: int,
        chunk_size: int,
        key_version: str,
    ) -> bytes:
        return (
            f"vibecanvas:object:v2\0{key}\0{key_version}\0{index}\0"
            f"{plaintext_size}\0{chunk_size}"
        ).encode("utf-8")

    def write(self, file: BinaryIO, *, key: str, plaintext: bytes) -> None:
        self.write_stream(file, key=key, source=io.BytesIO(plaintext), size=len(plaintext))

    def write_stream(self, file: BinaryIO, *, key: str, source: BinaryIO, size: int) -> None:
        """Encrypt a known-length stream without retaining the whole file.

        The caller must publish the destination only after this method succeeds.
        Truncated or growing input is rejected rather than publishing partial data.
        """
        if size < 0:
            raise ValueError("plaintext size must be nonnegative")
        dek = bytearray(os.urandom(AES_KEY_BYTES))
        try:
            wrap_nonce = os.urandom(GCM_NONCE_BYTES)
            wrapped_dek = AESGCM(self._master_key).encrypt(
                wrap_nonce,
                bytes(dek),
                self._wrap_aad(key, self.key_version),
            )
            nonce_prefix = os.urandom(8)
            header = json.dumps(
                {
                    "chunk_size": self.chunk_size,
                    "key_version": self.key_version,
                    "nonce_prefix": _b64(nonce_prefix),
                    "plaintext_size": size,
                    "schema_version": 2,
                    "wrap_nonce": _b64(wrap_nonce),
                    "wrapped_dek": _b64(wrapped_dek),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            file.write(_PREFIX.pack(MAGIC, len(header)))
            file.write(header)
            aead = AESGCM(bytes(dek))
            for index, offset in enumerate(
                range(0, size, self.chunk_size)
            ):
                remaining = min(self.chunk_size, size - offset)
                chunk = bytearray()
                while remaining:
                    part = source.read(remaining)
                    if not part:
                        raise ValueError("plaintext stream ended before declared size")
                    if len(part) > remaining:
                        raise ValueError("plaintext stream exceeded requested read size")
                    chunk.extend(part)
                    remaining -= len(part)
                chunk = bytes(chunk)
                nonce = nonce_prefix + index.to_bytes(4, "big")
                file.write(aead.encrypt(
                    nonce,
                    chunk,
                    self._chunk_aad(
                        key,
                        index=index,
                        plaintext_size=size,
                        chunk_size=self.chunk_size,
                        key_version=self.key_version,
                    ),
                ))
            if source.read(1):
                raise ValueError("plaintext stream exceeds declared size")
        finally:
            wipe_bytes(dek)

    def read_header(self, file: BinaryIO) -> ObjectHeader:
        prefix = file.read(_PREFIX.size)
        if len(prefix) != _PREFIX.size:
            raise SecretIntegrityError("object container is truncated")
        magic, header_size = _PREFIX.unpack(prefix)
        if magic != MAGIC:
            raise SecretIntegrityError("object container is not encrypted")
        if not 1 <= header_size <= _MAX_HEADER_BYTES:
            raise SecretIntegrityError("object container header is invalid")
        encoded = file.read(header_size)
        if len(encoded) != header_size:
            raise SecretIntegrityError("object container is truncated")
        try:
            raw = json.loads(encoded)
            if raw.get("schema_version") != 2:
                raise ValueError
            plaintext_size = int(raw["plaintext_size"])
            chunk_size = int(raw["chunk_size"])
            key_version = str(raw["key_version"])
            nonce_prefix = _unb64(raw["nonce_prefix"])
            wrap_nonce = _unb64(raw["wrap_nonce"])
            wrapped_dek = _unb64(raw["wrapped_dek"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SecretIntegrityError("object container header is invalid") from exc
        if (
            plaintext_size < 0
            or not 64 * 1024 <= chunk_size <= 4 * 1024 * 1024
            or len(nonce_prefix) != 8
            or len(wrap_nonce) != GCM_NONCE_BYTES
            or len(wrapped_dek) != AES_KEY_BYTES + GCM_TAG_BYTES
            or not key_version
        ):
            raise SecretIntegrityError("object container header is invalid")
        return ObjectHeader(
            plaintext_size=plaintext_size,
            chunk_size=chunk_size,
            nonce_prefix=nonce_prefix,
            wrapped_dek=wrapped_dek,
            wrap_nonce=wrap_nonce,
            key_version=key_version,
            data_offset=_PREFIX.size + header_size,
        )

    def _unwrap(self, *, key: str, header: ObjectHeader) -> bytearray:
        if header.key_version != self.key_version:
            raise SecretIntegrityError("object encryption key version is unavailable")
        try:
            return bytearray(AESGCM(self._master_key).decrypt(
                header.wrap_nonce,
                header.wrapped_dek,
                self._wrap_aad(key, header.key_version),
            ))
        except Exception as exc:
            raise SecretIntegrityError("object key failed integrity check") from exc

    def iter_range(
        self,
        file: BinaryIO,
        *,
        key: str,
        start: int = 0,
        end: int | None = None,
        output_chunk_size: int = 1024 * 1024,
    ) -> Iterator[bytes]:
        header = self.read_header(file)
        if start < 0 or (end is not None and end < 0):
            raise ValueError("object byte range must be non-negative")
        if output_chunk_size <= 0:
            raise ValueError("output chunk size must be positive")
        stop = (
            header.plaintext_size
            if end is None
            else min(header.plaintext_size, end + 1)
        )
        if start >= stop or header.plaintext_size == 0:
            return
        first = start // header.chunk_size
        last = (stop - 1) // header.chunk_size
        dek = self._unwrap(key=key, header=header)
        pending = bytearray()
        try:
            aead = AESGCM(bytes(dek))
            for index in range(first, last + 1):
                plain_offset = index * header.chunk_size
                plain_length = min(
                    header.chunk_size,
                    header.plaintext_size - plain_offset,
                )
                cipher_offset = (
                    header.data_offset
                    + index * (header.chunk_size + _TAG_BYTES)
                )
                file.seek(cipher_offset)
                encrypted = file.read(plain_length + _TAG_BYTES)
                if len(encrypted) != plain_length + _TAG_BYTES:
                    raise SecretIntegrityError("object container is truncated")
                try:
                    chunk = aead.decrypt(
                        header.nonce_prefix + index.to_bytes(4, "big"),
                        encrypted,
                        self._chunk_aad(
                            key,
                            index=index,
                            plaintext_size=header.plaintext_size,
                            chunk_size=header.chunk_size,
                            key_version=header.key_version,
                        ),
                    )
                except Exception as exc:
                    raise SecretIntegrityError(
                        "object chunk failed integrity check"
                    ) from exc
                left = max(start, plain_offset) - plain_offset
                right = min(stop, plain_offset + plain_length) - plain_offset
                pending.extend(chunk[left:right])
                while len(pending) >= output_chunk_size:
                    yield bytes(pending[:output_chunk_size])
                    del pending[:output_chunk_size]
            if pending:
                yield bytes(pending)
        finally:
            wipe_bytes(dek)

    def read(self, file: BinaryIO, *, key: str) -> bytes:
        return b"".join(self.iter_range(file, key=key))
