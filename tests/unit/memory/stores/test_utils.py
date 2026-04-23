"""Tests for storage utilities."""

from framework.memory.stores.utils import sanitize_scope_key


class TestSanitizeScopeKey:
    def test_simple_key_unchanged(self):
        assert sanitize_scope_key("session_123") == "session_123"

    def test_replaces_special_chars(self):
        assert sanitize_scope_key("tenant:user") == "tenant_user"
        assert sanitize_scope_key("path/to/key") == "path_to_key"
        assert sanitize_scope_key("a\\b|c*d?") == "a_b_c_d_"

    def test_empty_key(self):
        assert sanitize_scope_key("") == "_empty_"

    def test_long_key_hashes(self):
        long_key = "a" * 200
        result = sanitize_scope_key(long_key)
        assert len(result) <= 100
