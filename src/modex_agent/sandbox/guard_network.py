"""Network security guard — SSRF detection for command strings.

Scans commands for URLs targeting private/internal addresses.

The guard is **static-form only**: literal IPs are checked against the
blocked/allowed network lists; non-literal hostnames are left to the
execution layer. Resolving DNS at guard time would be a TOCTOU race
(DNS rebinding between check and fetch), so the guard never resolves.
"""

from __future__ import annotations

import ipaddress
import re
from contextlib import suppress
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, field_validator

from .guard import CommandGuard, CommandSeverity, GuardMatch, GuardResult

# Private and local address ranges blocked unless explicitly allowed.
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),  # Current network
    ipaddress.ip_network("10.0.0.0/8"),  # RFC1918 private
    ipaddress.ip_network("100.64.0.0/10"),  # Carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),  # Loopback
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local / cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),  # RFC1918 private
    ipaddress.ip_network("192.168.0.0/16"),  # RFC1918 private
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
)

# URL extraction regex for command strings
_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+", re.IGNORECASE)


def _normalize_addr(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Normalize IPv6-mapped IPv4 addresses.

    ``::ffff:127.0.0.1`` is treated as ``127.0.0.1`` for blocklist checks.
    """
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _is_private(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
    allowed_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (),
) -> bool:
    """Check if *addr* is in the blocked network list."""
    normalized = _normalize_addr(addr)
    if allowed_networks and any(normalized in net for net in allowed_networks):
        return False
    return any(normalized in net for net in _BLOCKED_NETWORKS)


def _is_allowed_loopback_target(
    hostname: str,
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Check if *hostname* is a narrow, explicitly allowed loopback target.

    Only literal ``localhost`` and literal loopback IPs are allowed.
    Public DNS names (whose resolution is not the guard's concern) are
    never loopback-allowed.
    """
    if not _normalize_addr(addr).is_loopback:
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    with suppress(ValueError):
        return ipaddress.ip_address(normalized).is_loopback
    return False


def _compile_networks(
    cidrs: tuple[str, ...],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return tuple(ipaddress.ip_network(cidr, strict=False) for cidr in cidrs)


def validate_url_target(
    url: str,
    *,
    allow_loopback: bool = False,
    allowed_networks: tuple[str, ...] = (),
) -> tuple[bool, str]:
    """Validate a URL target by static form only (no DNS resolution).

    Literal IP hosts are checked against the blocked network list (minus
    ``allowed_networks``, CIDR strings). Non-literal hostnames pass
    statically — resolution-time checks belong to the execution layer
    (TOCTOU-safe division of labor).

    Returns ``(ok, error_message)``. When *ok* is True, error_message is empty.
    """
    try:
        p = urlparse(url)
    except ValueError as e:
        return False, str(e)

    if p.scheme not in ("http", "https"):
        return True, ""  # Non-HTTP URLs are not our concern here

    hostname = p.hostname
    if not hostname:
        return True, ""  # No hostname, skip

    # Strip brackets from IPv6 literals ([::1] -> ::1).
    host_literal = hostname[1:-1] if hostname.startswith("[") else hostname
    try:
        addr = ipaddress.ip_address(host_literal)
    except ValueError:
        return True, ""  # Not a literal IP — execution layer's concern

    if allow_loopback and _is_allowed_loopback_target(hostname, addr):
        return True, ""

    if _is_private(addr, _compile_networks(allowed_networks)):
        return False, f"Blocked: {hostname} is a private/internal address"

    return True, ""


class NetworkGuardConfig(BaseModel):
    """Configuration for NetworkGuard (frozen pydantic value object).

    ``allowed_networks`` replaces the old process-global whitelist —
    CIDR ranges here bypass SSRF blocking for this guard instance only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    allow_loopback: bool = False
    allowed_networks: tuple[str, ...] = ()

    @field_validator("allowed_networks")
    @classmethod
    def _validate_cidrs(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for cidr in v:
            ipaddress.ip_network(cidr, strict=False)  # raises ValueError if invalid
        return v


class AddressDecision(BaseModel):
    """One address/URL judgment — typed result for execution-time reuse.

    ``reason`` is empty when allowed; otherwise it carries the guard's
    source-fact wording (e.g. ``Blocked: 10.0.0.5 is a private/internal
    address``) for the caller's own presentation copy.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    reason: str = ""


class NetworkGuard(CommandGuard):
    """SSRF detection guard for command strings.

    Scans commands for URLs and validates they do not target
    private/internal addresses. Static form checks only — never resolves
    DNS (resolution at check time races the fetch at execute time).
    :meth:`evaluate_address` / :meth:`evaluate_url` expose the same
    judgment as a typed decision so the execution-time fetch layer dials
    through the identical tables (one source of truth, no divergence).
    """

    def __init__(self, config: NetworkGuardConfig | None = None) -> None:
        self._config = config or NetworkGuardConfig()
        self._allowed_networks = _compile_networks(self._config.allowed_networks)

    def check(self, command: str) -> GuardResult:
        if not self._config.enabled:
            return GuardResult(allowed=True)

        for m in _URL_RE.finditer(command):
            decision = self.evaluate_url(m.group(0))
            if not decision.allowed:
                match = GuardMatch(
                    pattern="<ssrf>",
                    severity=CommandSeverity.CRITICAL,
                    category="ssrf",
                    description=f"SSRF: {decision.reason}",
                )
                return GuardResult(
                    allowed=False,
                    matches=(match,),
                    reason=f"Command denied: [critical] {decision.reason} (ssrf)",
                )

        return GuardResult(allowed=True)

    def evaluate_url(self, url: str) -> AddressDecision:
        """Judge a URL by static form — the typed twin of ``validate_url_target``.

        Non-literal hostnames pass (resolution belongs to the execution
        layer); literal IPs hit the shared blocked/allowed tables.
        """
        if not self._config.enabled:
            return AddressDecision(allowed=True)
        ok, error = validate_url_target(
            url,
            allow_loopback=self._config.allow_loopback,
            allowed_networks=self._config.allowed_networks,
        )
        return AddressDecision(allowed=ok, reason="" if ok else error)

    def evaluate_address(self, address: str) -> AddressDecision:
        """Judge one resolved IP — the seam execution-time checks dial through.

        Shares the blocked/allowed tables with the static checks, so
        connect-time validation and static validation can never diverge.
        Invalid input strings are denied (fail closed).
        """
        if not self._config.enabled:
            return AddressDecision(allowed=True)
        try:
            addr = ipaddress.ip_address(address)
        except ValueError:
            return AddressDecision(
                allowed=False, reason=f"Blocked: {address} is not a valid IP address"
            )
        if self._config.allow_loopback and _is_allowed_loopback_target(address, addr):
            return AddressDecision(allowed=True)
        if _is_private(addr, self._allowed_networks):
            return AddressDecision(
                allowed=False,
                reason=f"Blocked: {address} is a private/internal address",
            )
        return AddressDecision(allowed=True)
