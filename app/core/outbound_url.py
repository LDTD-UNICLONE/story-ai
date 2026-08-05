import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import FrozenSet, Iterable, Optional, Set, Union
from urllib.parse import urljoin, urlparse

import httpx

from app.core.config import settings
from app.core.exceptions import AppException


@dataclass(frozen=True)
class ValidatedPublicUrl:
    url: str
    addresses: FrozenSet[str]


def resolve_public_http_url(
    url: str,
    *,
    allowed_hosts: Iterable[str] = (),
) -> ValidatedPublicUrl:
    normalized = str(url or "").strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AppException("媒体地址必须是有效的 http(s) URL", code=40013, status_code=400)
    if parsed.username or parsed.password:
        raise AppException("媒体地址不能包含用户凭证", code=40013, status_code=400)

    hostname = parsed.hostname.rstrip(".").lower()
    trusted_hosts = {host.rstrip(".").lower() for host in allowed_hosts if host}
    if trusted_hosts and hostname not in trusted_hosts:
        raise AppException("媒体地址不属于受信任的存储域名", code=40013, status_code=400)

    try:
        addresses = _resolve_addresses(hostname, parsed.port)
    except (OSError, ValueError) as exc:
        raise AppException("媒体地址无法解析", code=40013, status_code=400) from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise AppException("媒体地址不能指向内网或保留地址", code=40013, status_code=400)
    return ValidatedPublicUrl(
        url=normalized,
        addresses=frozenset(str(_canonical_ip(address)) for address in addresses),
    )


def validate_public_http_url(url: str, *, allowed_hosts: Iterable[str] = ()) -> str:
    return resolve_public_http_url(url, allowed_hosts=allowed_hosts).url


async def open_safe_http_response(
    client: httpx.AsyncClient,
    source_url: str,
    *,
    allowed_hosts: Iterable[str] = (),
    max_redirects: int = 5,
) -> httpx.Response:
    current_url = source_url
    trusted_hosts = tuple(allowed_hosts)
    for redirect_count in range(max_redirects + 1):
        validated = await asyncio.to_thread(
            resolve_public_http_url,
            current_url,
            allowed_hosts=trusted_hosts,
        )
        response = await client.send(client.build_request("GET", validated.url), stream=True)
        try:
            _validate_response_peer(response, validated.addresses)
        except Exception:
            await response.aclose()
            raise
        if not response.is_redirect:
            return response
        location = response.headers.get("location")
        await response.aclose()
        if not location:
            raise AppException("媒体重定向地址无效", code=50230, status_code=502)
        if redirect_count >= max_redirects:
            raise AppException("媒体重定向次数过多", code=50230, status_code=502)
        current_url = urljoin(validated.url, location)
    raise AppException("媒体重定向次数过多", code=50230, status_code=502)


def trusted_oss_hosts() -> Set[str]:
    hosts: Set[str] = set()
    if settings.oss_public_base_url:
        host = urlparse(settings.oss_public_base_url).hostname
        if host:
            hosts.add(host)
    endpoint_host = urlparse(settings.oss_endpoint).hostname
    if endpoint_host and settings.oss_bucket_name:
        hosts.add(f"{settings.oss_bucket_name}.{endpoint_host}")
    return hosts


IpAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


def _resolve_addresses(hostname: str, port: Optional[int]) -> Set[IpAddress]:
    try:
        return {ipaddress.ip_address(hostname)}
    except ValueError:
        pass
    return {
        ipaddress.ip_address(sockaddr[0])
        for _, _, _, _, sockaddr in socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM)
    }


def _validate_response_peer(response: httpx.Response, allowed_addresses: FrozenSet[str]) -> None:
    network_stream = response.extensions.get("network_stream")
    get_extra_info = getattr(network_stream, "get_extra_info", None)
    if not callable(get_extra_info):
        raise AppException("媒体实际连接地址无法验证", code=50230, status_code=502)
    try:
        server_address = get_extra_info("server_addr")
        host = server_address[0] if isinstance(server_address, (tuple, list)) else server_address
        actual_address = _canonical_ip(ipaddress.ip_address(str(host).split("%", 1)[0]))
    except Exception as exc:
        raise AppException("媒体实际连接地址无法验证", code=50230, status_code=502) from exc
    if not actual_address.is_global or str(actual_address) not in allowed_addresses:
        raise AppException("媒体实际连接地址与安全解析结果不一致", code=50230, status_code=502)


def _canonical_ip(address: IpAddress) -> IpAddress:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address
