from __future__ import annotations

import pytest

from framework.sandbox.guard import CommandSeverity, GuardMatch, GuardResult
from framework.sandbox.guard_network import (
    NetworkGuard,
    NetworkGuardConfig,
    configure_ssrf_whitelist,
    validate_url_target,
)


class TestNetworkGuardBlocked:
    """Private/internal addresses should be blocked."""

    @pytest.fixture
    def guard(self) -> NetworkGuard:
        return NetworkGuard()

    def test_loopback_blocked(self, guard: NetworkGuard) -> None:
        result = guard.check("curl http://127.0.0.1")
        assert not result.allowed
        assert len(result.matches) == 1
        match = result.matches[0]
        assert match.severity == CommandSeverity.CRITICAL
        assert match.category == "ssrf"
        assert "127.0.0.1" in match.description

    def test_localhost_blocked(self, guard: NetworkGuard) -> None:
        result = guard.check("wget http://localhost")
        assert not result.allowed
        assert len(result.matches) == 1
        match = result.matches[0]
        assert match.severity == CommandSeverity.CRITICAL
        assert match.category == "ssrf"

    def test_rfc1918_blocked(self, guard: NetworkGuard) -> None:
        result = guard.check("curl http://192.168.1.1")
        assert not result.allowed
        assert len(result.matches) == 1
        match = result.matches[0]
        assert "192.168.1.1" in match.description

    def test_link_local_blocked(self, guard: NetworkGuard) -> None:
        result = guard.check("curl http://169.254.169.254")
        assert not result.allowed
        assert len(result.matches) == 1
        match = result.matches[0]
        assert "169.254.169.254" in match.description

    def test_ipv6_loopback_blocked(self, guard: NetworkGuard) -> None:
        result = guard.check("curl http://[::1]")
        assert not result.allowed
        assert len(result.matches) == 1
        match = result.matches[0]
        assert "::1" in match.description


class TestNetworkGuardAllowed:
    """Public addresses and safe commands should pass."""

    @pytest.fixture
    def guard(self) -> NetworkGuard:
        return NetworkGuard()

    def test_public_allowed(self, guard: NetworkGuard) -> None:
        result = guard.check("curl http://example.com")
        assert result.allowed
        assert result.matches == ()
        assert result.reason is None

    def test_no_url_allowed(self, guard: NetworkGuard) -> None:
        result = guard.check("echo hello")
        assert result.allowed
        assert result.matches == ()
        assert result.reason is None


class TestNetworkGuardConfig:
    """NetworkGuardConfig options affect behavior."""

    def test_disabled_guard(self) -> None:
        config = NetworkGuardConfig(enabled=False)
        guard = NetworkGuard(config)
        result = guard.check("curl http://127.0.0.1")
        assert result.allowed
        assert result.matches == ()
        assert result.reason is None

    def test_loopback_allowed_when_configured(self) -> None:
        config = NetworkGuardConfig(allow_loopback=True)
        guard = NetworkGuard(config)
        result = guard.check("curl http://localhost")
        assert result.allowed
        assert result.matches == ()
        assert result.reason is None

    def test_loopback_ip_allowed_when_configured(self) -> None:
        config = NetworkGuardConfig(allow_loopback=True)
        guard = NetworkGuard(config)
        result = guard.check("curl http://127.0.0.1")
        assert result.allowed
        assert result.matches == ()
        assert result.reason is None

    def test_ipv6_loopback_allowed_when_configured(self) -> None:
        config = NetworkGuardConfig(allow_loopback=True)
        guard = NetworkGuard(config)
        result = guard.check("curl http://[::1]")
        assert result.allowed
        assert result.matches == ()
        assert result.reason is None


class TestNetworkGuardMultipleUrls:
    """Commands with multiple URLs."""

    def test_multiple_urls_one_blocked(self) -> None:
        guard = NetworkGuard()
        result = guard.check("curl http://example.com http://127.0.0.1")
        assert not result.allowed
        assert len(result.matches) == 1
        match = result.matches[0]
        assert match.severity == CommandSeverity.CRITICAL
        assert match.category == "ssrf"
        assert "127.0.0.1" in match.description

    def test_multiple_urls_all_public(self) -> None:
        guard = NetworkGuard()
        result = guard.check("curl http://example.com http://google.com")
        assert result.allowed
        assert result.matches == ()


class TestNetworkGuardResultStructure:
    """GuardResult and GuardMatch structure for NetworkGuard."""

    def test_allowed_result(self) -> None:
        guard = NetworkGuard()
        result = guard.check("echo hello")
        assert result.allowed is True
        assert result.matches == ()
        assert result.reason is None

    def test_denied_result_has_matches(self) -> None:
        guard = NetworkGuard()
        result = guard.check("curl http://127.0.0.1")
        assert result.allowed is False
        assert len(result.matches) == 1
        assert result.reason is not None
        for match in result.matches:
            assert isinstance(match, GuardMatch)
            assert match.pattern == "<ssrf>"
            assert match.category == "ssrf"
            assert match.description


class TestValidateUrlTarget:
    """Direct tests for validate_url_target function."""

    def test_public_url_ok(self) -> None:
        ok, error = validate_url_target("http://example.com")
        assert ok is True
        assert error == ""

    def test_loopback_blocked(self) -> None:
        ok, error = validate_url_target("http://127.0.0.1")
        assert ok is False
        assert "127.0.0.1" in error

    def test_localhost_blocked(self) -> None:
        ok, error = validate_url_target("http://localhost")
        assert ok is False
        assert "localhost" in error

    def test_loopback_allowed(self) -> None:
        ok, error = validate_url_target("http://localhost", allow_loopback=True)
        assert ok is True
        assert error == ""

    def test_non_http_skipped(self) -> None:
        ok, error = validate_url_target("ftp://127.0.0.1/file")
        assert ok is True
        assert error == ""

    def test_no_hostname_skipped(self) -> None:
        ok, error = validate_url_target("http:///path")
        assert ok is True
        assert error == ""


class TestConfigureSsrfWhitelist:
    """Tests for configure_ssrf_whitelist."""

    def test_whitelist_allows_blocked_network(self) -> None:
        configure_ssrf_whitelist(["127.0.0.0/8"])
        ok, error = validate_url_target("http://127.0.0.1")
        assert ok is True
        assert error == ""
        # Reset whitelist
        configure_ssrf_whitelist([])

    def test_whitelist_invalid_cidr_ignored(self) -> None:
        configure_ssrf_whitelist(["invalid", "192.168.0.0/16"])
        ok, error = validate_url_target("http://192.168.1.1")
        assert ok is True
        assert error == ""
        # Reset whitelist
        configure_ssrf_whitelist([])
