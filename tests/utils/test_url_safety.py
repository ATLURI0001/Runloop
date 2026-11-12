from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.utils import url_safety


@pytest.mark.asyncio
async def test_request_with_safe_redirects_applies_custom_validator_to_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = [(None, None, None, None, ("1.1.1.1", 0))]
    monkeypatch.setattr(
        url_safety,
        "resolve_and_validate",
        lambda url: (True, "", "allowed.example", resolved),
    )
    redirect = MagicMock(status_code=302, headers={"Location": "https://blocked.example/file"})
    client = MagicMock()
    client.request = AsyncMock(return_value=redirect)

    response, blocked = await url_safety.request_with_safe_redirects(
        client,
        "GET",
        "https://allowed.example/file",
        validate_url=lambda url: (
            (True, "") if url.startswith("https://allowed.example/") else (False, "blocked host")
        ),
    )

    assert response is None
    assert blocked is not None
    assert blocked["content"] == "Request blocked: blocked host"
    client.request.assert_awaited_once()


def test_resolve_and_validate_blocks_ipv4_compatible_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    # IPv4-compatible IPv6 addresses like ::127.0.0.1 or ::169.254.169.254
    for blocked_ip in ["::127.0.0.1", "::169.254.169.254", "::10.0.0.1"]:
        monkeypatch.setattr(
            url_safety.socket,
            "getaddrinfo",
            lambda host, port, *args, **kwargs: [(None, None, None, None, (blocked_ip, 0))],
        )
        is_safe, reason, host, _ = url_safety.resolve_and_validate("https://attacker.example/data")
        assert not is_safe, f"Expected {blocked_ip} to be blocked"
        assert "blocked address" in reason


def test_resolve_and_validate_blocks_6to4_encapsulated_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 2002:7f00:0001:: encapsulates 127.0.0.1 in 6to4
    monkeypatch.setattr(
        url_safety.socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [(None, None, None, None, ("2002:7f00:0001::", 0))],
    )
    is_safe, reason, host, _ = url_safety.resolve_and_validate("https://attacker.example/data")
    assert not is_safe
    assert "blocked address" in reason


def test_resolve_and_validate_blocks_ipv4_mapped_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        url_safety.socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [(None, None, None, None, ("::ffff:127.0.0.1", 0))],
    )
    is_safe, reason, host, _ = url_safety.resolve_and_validate("https://attacker.example/data")
    assert not is_safe
    assert "blocked address" in reason


def test_resolve_and_validate_allows_public_ipv6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        url_safety.socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (None, None, None, None, ("2607:f8b0:4005:805::200e", 0))
        ],
    )
    is_safe, reason, host, _ = url_safety.resolve_and_validate("https://public.example/data")
    assert is_safe
    assert reason == ""


def test_resolve_and_validate_blocks_nat64_private_and_allows_case_insensitive_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for blocked_ip in ["64:ff9b::127.0.0.1", "64:ff9b::169.254.169.254", "64:ff9b:1::10.0.0.1"]:
        monkeypatch.setattr(
            url_safety.socket,
            "getaddrinfo",
            lambda host, port, *args, **kwargs: [(None, None, None, None, (blocked_ip, 0))],
        )
        is_safe, reason, _, _ = url_safety.resolve_and_validate("HTTPS://attacker.example/data")
        assert not is_safe, f"Expected NAT64 {blocked_ip} to be blocked"
        assert "blocked address" in reason


def test_pinned_url_strips_zone_id_and_host_header_omits_userinfo() -> None:
    from urllib.parse import urlparse

    assert url_safety.pinned_url("https://example.com:8443/path", "fe80::1%eth0") == "https://[fe80::1]:8443/path"
    parsed = urlparse("https://secret:pass@example.com:8443/path")
    assert url_safety._host_header_for(parsed) == "example.com:8443"
