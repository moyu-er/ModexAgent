from __future__ import annotations

import socket

import pytest
from pydantic import ValidationError

from modex_agent.sandbox.guard import CommandSeverity, GuardMatch
from modex_agent.sandbox.guard_network import (
    NetworkGuard,
    NetworkGuardConfig,
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

    def test_rfc1918_blocked(self, guard: NetworkGuard) -> None:
        result = guard.check("curl http://192.168.1.1")
        assert not result.allowed
        assert len(result.matches) == 1
        match = result.matches[0]
        assert "192.168.1.1" in match.description

    def test_dns_name_passes_static_check(self, guard: NetworkGuard) -> None:
        """``localhost`` is a DNS name, not a literal IP — the static guard
        leaves non-literal hostnames to the execution layer (no TOCTOU)."""
        result = guard.check("wget http://localhost")
        assert result.allowed

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

    def test_localhost_is_dns_name_passes_statically(self) -> None:
        """``localhost`` is a name, not a literal IP — static check only."""
        ok, error = validate_url_target("http://localhost")
        assert ok is True
        assert error == ""

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


class TestNetworkGuardStaticOnly:
    """The guard does static form checks only — DNS resolution is out.

    Resolving at guard time and fetching at execute time is a TOCTOU race
    (DNS rebinding), so the guard must never resolve.
    """

    def test_check_never_resolves_dns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fail(*args: object, **kwargs: object) -> object:
            raise AssertionError("guard must not resolve DNS (TOCTOU)")

        monkeypatch.setattr(socket, "getaddrinfo", _fail)
        guard = NetworkGuard()
        assert guard.check("curl http://example.com").allowed
        assert not guard.check("curl http://127.0.0.1").allowed

    def test_dns_name_form_passes_statically(self) -> None:
        """A non-literal hostname is the execution layer's concern."""
        ok, error = validate_url_target("http://internal.invalid")
        assert ok is True
        assert error == ""


class TestNetworkGuardWhitelistInjection:
    """Whitelist is injected via NetworkGuardConfig — no global state."""

    def test_whitelist_allows_blocked_network(self) -> None:
        config = NetworkGuardConfig(allowed_networks=("127.0.0.0/8",))
        guard = NetworkGuard(config)
        result = guard.check("curl http://127.0.0.1")
        assert result.allowed
        assert result.matches == ()
        assert result.reason is None

    def test_whitelist_via_validate_url_target(self) -> None:
        config = NetworkGuardConfig(allowed_networks=("192.168.0.0/16",))
        ok, error = validate_url_target(
            "http://192.168.1.1",
            allowed_networks=config.allowed_networks,
        )
        assert ok is True
        assert error == ""

    def test_invalid_cidr_rejected_by_config(self) -> None:
        with pytest.raises(ValidationError):
            NetworkGuardConfig(allowed_networks=("not-a-cidr",))

    def test_whitelist_not_shared_between_guards(self) -> None:
        """Injection replaces the old global — instances are independent."""
        allowed = NetworkGuard(NetworkGuardConfig(allowed_networks=("127.0.0.0/8",)))
        plain = NetworkGuard()
        assert allowed.check("curl http://127.0.0.1").allowed
        assert not plain.check("curl http://127.0.0.1").allowed


class TestNetworkGuardConfigPydantic:
    """NetworkGuardConfig is a frozen pydantic model."""

    def test_frozen(self) -> None:
        config = NetworkGuardConfig()
        with pytest.raises(ValidationError):
            config.enabled = False

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            NetworkGuardConfig(unknown_field=1)


class TestNetworkGuardEvaluateAddress:
    """The typed address-decision seam reused by static + execution checks.

    ``evaluate_address`` is the single place a resolved IP is judged —
    the execution-time SSRF layer dials through this same decision, so
    static literal checks and connect-time checks can never diverge.
    """

    def test_public_address_allowed(self) -> None:
        guard = NetworkGuard()
        decision = guard.evaluate_address("93.184.216.34")
        assert decision.allowed
        assert decision.reason == ""

    def test_private_address_denied_with_reason(self) -> None:
        guard = NetworkGuard()
        decision = guard.evaluate_address("192.168.1.10")
        assert not decision.allowed
        assert "192.168.1.10" in decision.reason
        assert "private" in decision.reason

    def test_metadata_address_denied(self) -> None:
        guard = NetworkGuard()
        decision = guard.evaluate_address("169.254.169.254")
        assert not decision.allowed

    def test_ipv6_loopback_denied(self) -> None:
        guard = NetworkGuard()
        decision = guard.evaluate_address("::1")
        assert not decision.allowed

    def test_ipv6_mapped_ipv4_denied(self) -> None:
        """``::ffff:127.0.0.1`` normalizes to 127.0.0.1 — no bypass."""
        guard = NetworkGuard()
        decision = guard.evaluate_address("::ffff:127.0.0.1")
        assert not decision.allowed

    def test_allowed_networks_bypass(self) -> None:
        guard = NetworkGuard(NetworkGuardConfig(allowed_networks=("10.0.0.0/8",)))
        assert guard.evaluate_address("10.1.2.3").allowed
        assert not guard.evaluate_address("192.168.0.1").allowed

    def test_allow_loopback_permits_literal_loopback(self) -> None:
        guard = NetworkGuard(NetworkGuardConfig(allow_loopback=True))
        assert guard.evaluate_address("127.0.0.1").allowed

    def test_disabled_config_allows_everything(self) -> None:
        guard = NetworkGuard(NetworkGuardConfig(enabled=False))
        assert guard.evaluate_address("127.0.0.1").allowed

    def test_invalid_ip_string_denied(self) -> None:
        decision = NetworkGuard().evaluate_address("not-an-ip")
        assert not decision.allowed
        assert "not-an-ip" in decision.reason


class TestNetworkGuardEvaluateUrl:
    """The typed URL-form seam: same verdict as check(), structured."""

    def test_public_url_allowed(self) -> None:
        decision = NetworkGuard().evaluate_url("http://example.com")
        assert decision.allowed
        assert decision.reason == ""

    def test_literal_private_url_denied(self) -> None:
        decision = NetworkGuard().evaluate_url("http://10.0.0.5/x")
        assert not decision.allowed
        assert "10.0.0.5" in decision.reason

    def test_non_literal_hostname_deferred(self) -> None:
        """Names are the execution layer's concern — statically allowed."""
        decision = NetworkGuard().evaluate_url("http://internal.invalid")
        assert decision.allowed

    def test_matches_check_verdict(self) -> None:
        guard = NetworkGuard()
        ok, _error = validate_url_target("http://192.168.0.9")
        decision = guard.evaluate_url("http://192.168.0.9")
        assert ok == decision.allowed

    def test_non_http_scheme_ignored(self) -> None:
        decision = NetworkGuard().evaluate_url("ftp://127.0.0.1/f")
        assert decision.allowed

    def test_malformed_url_denied_not_raised(self) -> None:
        decision = NetworkGuard().evaluate_url("http://[::1")
        assert not decision.allowed
