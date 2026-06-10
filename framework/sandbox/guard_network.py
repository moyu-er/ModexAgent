"""Network security guard — SSRF detection for command strings.

Scans commands for URLs targeting private/internal addresses.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import urlparse

from .guard import CommandSeverity, GuardMatch, GuardResult


# Blocked private/internal networks (same as nanobot)
_BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("0.0.0.0/8"),         # Current network
    ipaddress.ip_network("10.0.0.0/8"),        # RFC1918 private
    ipaddress.ip_network("100.64.0.0/10"),     # Carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),       # Loopback
    ipaddress.ip_network("169.254.0.0/16"),    # Link-local / cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),     # RFC1918 private
    ipaddress.ip_network("192.168.0.0/16"),    # RFC1918 private
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]

# URL extraction regex for command strings
_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+", re.IGNORECASE)

# Whitelist (user-configurable)
_allowed_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []


def configure_ssrf_whitelist(cidrs: list[str]) -> None:
    """Allow specific CIDR ranges to bypass SSRF blocking."""
    global _allowed_networks
    nets = []
    for cidr in cidrs:
        with suppress(ValueError):
            nets.append(ipaddress.ip_network(cidr, strict=False))
    _allowed_networks = nets


def _normalize_addr(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Normalize IPv6-mapped IPv4 addresses.

    ``::ffff:127.0.0.1`` is treated as ``127.0.0.1`` for blocklist checks.
    """
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if *addr* is in the blocked network list."""
    normalized = _normalize_addr(addr)
    if _allowed_networks and any(normalized in net for net in _allowed_networks):
        return False
    return any(normalized in net for net in _BLOCKED_NETWORKS)


def _is_allowed_loopback_target(
    hostname: str,
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address],
) -> bool:
    """Check if *hostname* is a narrow, explicitly allowed loopback target.

    Only literal ``localhost`` and literal loopback IPs are allowed.
    Public DNS names resolving to loopback are NOT allowed.
    """
    if not addrs or not all(_normalize_addr(a).is_loopback for a in addrs):
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    with suppress(ValueError):
        return ipaddress.ip_address(hostname).is_loopback
    return False


def validate_url_target(url: str, *, allow_loopback: bool = False) -> tuple[bool, str]:
    """Validate a URL target is safe (not private/internal).

    Returns ``(ok, error_message)``. When *ok* is True, error_message is empty.
    """
    try:
        p = urlparse(url)
    except Exception as e:
        return False, str(e)

    if p.scheme not in ("http", "https"):
        return True, ""  # Non-HTTP URLs are not our concern here

    hostname = p.hostname
    if not hostname:
        return True, ""  # No hostname, skip

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return True, ""  # Cannot resolve, skip (will fail at fetch time)

    addrs = []
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        addrs.append(addr)

    if allow_loopback and _is_allowed_loopback_target(hostname, addrs):
        return True, ""

    for addr in addrs:
        if _is_private(addr):
            return False, f"Blocked: {hostname} resolves to private/internal address {addr}"

    return True, ""


@dataclass
class NetworkGuardConfig:
    enabled: bool = True
    allow_loopback: bool = False


class NetworkGuard:
    """SSRF detection guard for command strings.

    Scans commands for URLs and validates they do not target private/internal
    addresses.
    """

    def __init__(self, config: NetworkGuardConfig | None = None) -> None:
        self._config = config or NetworkGuardConfig()

    def check(self, command: str) -> GuardResult:
        if not self._config.enabled:
            return GuardResult(allowed=True)

        for m in _URL_RE.finditer(command):
            url = m.group(0)
            ok, error = validate_url_target(url, allow_loopback=self._config.allow_loopback)
            if not ok:
                match = GuardMatch(
                    pattern="<ssrf>",
                    severity=CommandSeverity.CRITICAL,
                    category="ssrf",
                    description=f"SSRF: {error}",
                )
                return GuardResult(
                    allowed=False,
                    matches=(match,),
                    reason=f"Command denied: [critical] {error} (ssrf)",
                )

        return GuardResult(allowed=True)
