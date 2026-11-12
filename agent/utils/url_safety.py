import ipaddress
import socket
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx2

_MAX_REDIRECTS = 5
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_ENTITY_HEADERS = {"content-encoding", "content-language", "content-length", "content-type"}
_SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization"}


_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL_USE = ipaddress.IPv6Network("64:ff9b:1::/48")


def _extract_embedded_ipv4_from_nat64(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Extract the embedded IPv4 address from RFC 6052 / RFC 8215 NAT64 prefixes."""
    stripped = ipaddress.IPv6Address(int(ip))
    if stripped in _NAT64_WELL_KNOWN or stripped in _NAT64_LOCAL_USE:
        return ipaddress.IPv4Address(int(stripped) & 0xFFFFFFFF)
    return None


def _host_header_for(parsed: Any) -> str:
    """Format an HTTP Host header without leaking credentials from netloc."""
    hostname = parsed.hostname or ""
    host_literal = f"[{hostname}]" if ":" in hostname else hostname
    return f"{host_literal}:{parsed.port}" if parsed.port else host_literal


def resolve_and_validate(url: str) -> tuple[bool, str, str | None, list | None]:
    """Resolve a URL host and require every resolved address to be public."""
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            return False, f"Unsupported URL scheme: {parsed.scheme or '<missing>'}", None, None

        hostname = parsed.hostname
        if not hostname:
            return False, "Could not parse hostname from URL", None, None

        try:
            addr_infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            return False, f"Could not resolve hostname: {hostname}", hostname, None

        if not addr_infos:
            return False, f"Could not resolve hostname: {hostname}", hostname, None

        for addr_info in addr_infos:
            ip_str = addr_info[4][0]
            try:
                ip = ipaddress.ip_address(ip_str.split("%", 1)[0])
            except ValueError:
                return False, f"Could not parse resolved address: {ip_str}", hostname, None

            if isinstance(ip, ipaddress.IPv6Address):
                if ip.ipv4_mapped is not None:
                    ip = ip.ipv4_mapped
                elif int(ip) <= 0xFFFFFFFF:
                    # Deprecated IPv4-compatible IPv6 address (::x.x.x.x)
                    ip = ipaddress.IPv4Address(int(ip))
                elif (embedded := _extract_embedded_ipv4_from_nat64(ip)) is not None:
                    ip = embedded
                elif ip.sixtofour is not None:
                    # 6to4 encapsulation (2002:x.x.x.x::)
                    if not ip.sixtofour.is_global:
                        return False, f"URL resolves to blocked address: {ip_str}", hostname, None
                elif ip.teredo is not None:
                    # Teredo encapsulation (2001:0000::)
                    server, client = ip.teredo
                    if not server.is_global or not client.is_global:
                        return False, f"URL resolves to blocked address: {ip_str}", hostname, None

            if not ip.is_global:
                return False, f"URL resolves to blocked address: {ip_str}", hostname, None

        return True, "", hostname, addr_infos
    except Exception as e:
        return False, f"URL validation error: {e}", None, None


def is_url_safe(url: str) -> tuple[bool, str]:
    """Check if a URL is safe to request."""
    is_safe, reason, _, _ = resolve_and_validate(url)
    return is_safe, reason


def _blocked_response(url: str, reason: str) -> dict[str, Any]:
    return {
        "success": False,
        "status_code": 0,
        "headers": {},
        "content": f"Request blocked: {reason}",
        "url": url,
    }


def pinned_url(url: str, ip: str) -> str:
    parsed = urlparse(url)
    clean_ip = ip.split("%", 1)[0].strip("[]")
    host_literal = f"[{clean_ip}]" if ":" in clean_ip else clean_ip
    netloc = f"{host_literal}:{parsed.port}" if parsed.port else host_literal
    return urlunparse(parsed._replace(netloc=netloc))


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError:
        port = -1
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def _is_sensitive_header(name: str) -> bool:
    normalized = name.lower()
    return (
        normalized in _SENSITIVE_HEADERS or "api-key" in normalized or normalized.endswith("-token")
    )


def _redirect_method(method: str, status_code: int) -> str:
    if status_code == 303 and method != "HEAD":
        return "GET"
    if status_code == 302 and method != "HEAD":
        return "GET"
    if status_code == 301 and method == "POST":
        return "GET"
    return method


async def request_with_safe_redirects(
    client: httpx2.AsyncClient,
    method: str,
    url: str,
    *,
    headers_for_url: Callable[[str, str], Mapping[str, str] | None] | None = None,
    validate_url: Callable[[str], tuple[bool, str]] | None = None,
    stream: bool = False,
    **kwargs: Any,
) -> tuple[httpx2.Response | None, dict[str, Any] | None]:
    """Issue a request with DNS pinning and per-hop redirect validation.

    With ``stream=True``, the caller must close the returned response.
    Redirect responses are closed without reading their bodies.
    """
    current_method = method.upper()
    current_url = url
    request_kwargs = dict(kwargs)
    caller_headers = dict(request_kwargs.pop("headers", None) or {})
    caller_extensions = dict(request_kwargs.pop("extensions", None) or {})
    response: httpx2.Response | None = None

    for redirect_count in range(_MAX_REDIRECTS + 1):
        if validate_url:
            allowed, reason = validate_url(current_url)
            if not allowed:
                return None, _blocked_response(current_url, reason)
        is_safe, reason, hostname, addr_infos = resolve_and_validate(current_url)
        if not is_safe or hostname is None or addr_infos is None:
            return None, _blocked_response(current_url, reason)

        parsed = urlparse(current_url)
        per_hop_headers = dict(headers_for_url(url, current_url) or {}) if headers_for_url else {}
        headers = {**caller_headers, **per_hop_headers, "Host": _host_header_for(parsed)}
        extensions = {**caller_extensions, "sni_hostname": hostname}

        pinned_ips = list(dict.fromkeys(addr_info[4][0] for addr_info in addr_infos))
        for address_index, pinned_ip in enumerate(pinned_ips):
            try:
                if stream:
                    build_kwargs = dict(request_kwargs)
                    send_kwargs = (
                        {"auth": build_kwargs.pop("auth")} if "auth" in build_kwargs else {}
                    )
                    request = client.build_request(
                        current_method,
                        pinned_url(current_url, pinned_ip),
                        headers=headers,
                        extensions=extensions,
                        **build_kwargs,
                    )
                    response = await client.send(
                        request, stream=True, follow_redirects=False, **send_kwargs
                    )
                else:
                    response = await client.request(
                        current_method,
                        pinned_url(current_url, pinned_ip),
                        follow_redirects=False,
                        headers=headers,
                        extensions=extensions,
                        **request_kwargs,
                    )
                break
            except (httpx2.ConnectError, httpx2.ConnectTimeout):
                if address_index == len(pinned_ips) - 1:
                    raise

        if response is None:
            raise httpx2.ConnectError("No response received from pinned address")
        if response.status_code not in _REDIRECT_CODES:
            return response, None

        location = response.headers.get("Location")
        if not location:
            return response, None

        if stream:
            await response.aclose()

        if redirect_count == _MAX_REDIRECTS:
            return None, _blocked_response(current_url, "Too many redirects")

        next_url = urljoin(current_url, location)
        request_kwargs.pop("params", None)
        if _origin(current_url) != _origin(next_url):
            caller_headers = {
                name: value
                for name, value in caller_headers.items()
                if not _is_sensitive_header(name)
            }
            request_kwargs.pop("auth", None)
            request_kwargs.pop("cookies", None)

        next_method = _redirect_method(current_method, response.status_code)
        if next_method != current_method:
            caller_headers = {
                name: value
                for name, value in caller_headers.items()
                if name.lower() not in _ENTITY_HEADERS
            }
            request_kwargs.pop("data", None)
            request_kwargs.pop("content", None)
            request_kwargs.pop("json", None)

        current_method = next_method
        current_url = next_url

    return None, _blocked_response(current_url, "Too many redirects")
