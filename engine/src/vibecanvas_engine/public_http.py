"""Bounded HTTP downloads for user-provided public URLs.

The resolver returns every public DNS answer and the connection is made to one
of those exact addresses while TLS still authenticates the original hostname.
Redirects remain supported, but every hop is independently validated and
pinned before a socket is opened.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import urllib3


class PublicHttpError(ValueError):
    """Raised when a remote URL or response violates the egress boundary."""


@dataclass(frozen=True, slots=True)
class PublicHttpTarget:
    url: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return bool(address.is_global)


def validate_public_https_url(value: str, *, label: str = "URL") -> PublicHttpTarget:
    """Validate a public HTTPS URL and return the DNS answers just checked."""

    raw = str(value or "").strip()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise PublicHttpError(f"{label} is malformed") from exc
    if parts.scheme.lower() != "https":
        raise PublicHttpError(f"{label} must be an absolute public HTTPS URL")
    if (
        not parts.hostname
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
    ):
        raise PublicHttpError(
            f"{label} must be an absolute public URL without embedded credentials"
        )

    hostname = parts.hostname.rstrip(".").lower()
    if not hostname or hostname == "localhost" or hostname.endswith(".localhost"):
        raise PublicHttpError(f"{label} must not target localhost")
    resolved_port = port or 443
    try:
        answers = socket.getaddrinfo(
            hostname,
            resolved_port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise PublicHttpError(f"{label} host could not be resolved") from exc

    addresses = tuple(dict.fromkeys(str(answer[4][0]) for answer in answers))
    if not addresses:
        raise PublicHttpError(f"{label} host returned no usable addresses")
    if any(not _is_public_address(address) for address in addresses):
        raise PublicHttpError(
            f"{label} must not resolve to a private, local, reserved, or "
            "non-routable address"
        )
    return PublicHttpTarget(
        url=urlunsplit(("https", parts.netloc, parts.path, parts.query, "")),
        hostname=hostname,
        port=resolved_port,
        addresses=addresses,
    )


def _host_header(target: PublicHttpTarget) -> str:
    hostname = (
        f"[{target.hostname}]" if ":" in target.hostname else target.hostname
    )
    return hostname if target.port == 443 else f"{hostname}:{target.port}"


def _request_target(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(("", "", parts.path or "/", parts.query, ""))


def download_public_bytes(
    url: str,
    *,
    max_bytes: int,
    max_redirects: int = 4,
    connect_timeout: float = 5.0,
    read_timeout: float = 20.0,
    label: str = "remote resource",
) -> bytes:
    """Download a bounded public HTTPS resource with safe redirect support."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    current_url = str(url or "")
    visited: set[str] = set()
    for hop in range(max_redirects + 1):
        target = validate_public_https_url(current_url, label=label)
        if target.url in visited:
            raise PublicHttpError(f"{label} redirect loop detected")
        visited.add(target.url)

        response = None
        last_error: BaseException | None = None
        for address in target.addresses:
            pool = urllib3.HTTPSConnectionPool(
                address,
                target.port,
                timeout=urllib3.Timeout(
                    connect=connect_timeout,
                    read=read_timeout,
                ),
                retries=False,
                cert_reqs="CERT_REQUIRED",
                assert_hostname=target.hostname,
                server_hostname=target.hostname,
            )
            try:
                response = pool.request(
                    "GET",
                    _request_target(target.url),
                    headers={
                        "Accept": "image/*, application/octet-stream;q=0.8",
                        "Host": _host_header(target),
                        "User-Agent": "flowork/1.0 remote media fetcher",
                    },
                    redirect=False,
                    preload_content=False,
                )
                break
            except Exception as exc:
                last_error = exc
                pool.close()
        if response is None:
            raise PublicHttpError(f"{label} could not be loaded") from last_error

        try:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise PublicHttpError(f"{label} returned an invalid redirect")
                if hop >= max_redirects:
                    raise PublicHttpError(f"{label} redirected too many times")
                current_url = urljoin(target.url, location)
                continue
            if response.status < 200 or response.status >= 300:
                raise PublicHttpError(
                    f"{label} returned HTTP status {response.status}"
                )
            declared_size = int(response.headers.get("content-length") or 0)
            if declared_size > max_bytes:
                raise PublicHttpError(f"{label} is too large")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(min(64 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise PublicHttpError(f"{label} is too large")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            response.release_conn()
            response.close()
            pool.close()

    raise PublicHttpError(f"{label} redirected too many times")


__all__ = [
    "PublicHttpError",
    "PublicHttpTarget",
    "download_public_bytes",
    "validate_public_https_url",
]
