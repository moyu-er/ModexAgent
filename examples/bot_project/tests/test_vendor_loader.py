"""Tests for the offline cl100k_base loader: integrity, recovery, and parity.

These exercise the safety net (sha256 verification + re-download) without
hitting the network: ``_download_to`` is patched to restore from the valid
committed blob, and the real blob is the source of canonical bytes.
"""
from __future__ import annotations

import ssl
import urllib.request
from pathlib import Path

import pytest
from bot.memory import vendor_loader as vl
from bot.memory.vendor_loader import (
    EXPECTED_SHA256,
    VENDOR_BLOB,
    _sha256,
    load_cl100k,
)


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    # load_cl100k is lru_cached — isolate every test.
    load_cl100k.cache_clear()


def test_vendored_blob_passes_checksum() -> None:
    """The committed blob must match the pinned sha256."""
    assert VENDOR_BLOB.is_file()
    assert _sha256(VENDOR_BLOB) == EXPECTED_SHA256


def test_load_known_counts_and_vocab() -> None:
    enc = load_cl100k()
    assert enc.name == "cl100k_base"
    assert enc.n_vocab == 100277
    assert len(enc.encode("hello world")) == 2
    assert len(enc.encode("")) == 0


def test_corrupt_blob_triggers_redownload_and_recovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tampered blob is detected and repaired via re-download."""
    # Tampered target (wrong content) where the loader will look.
    corrupt = tmp_path / vl.BLOB_FILENAME
    corrupt.write_bytes(b"garbage that is not a valid tiktoken bpe file\n")
    monkeypatch.setattr(vl, "VENDOR_BLOB", corrupt)
    monkeypatch.setattr(vl, "_VENDOR_DIR", tmp_path)

    repaired = {"called": False}

    def fake_download(target: Path) -> None:
        repaired["called"] = True
        # Restore canonical bytes (no network).
        target.write_bytes(VENDOR_BLOB.read_bytes())

    monkeypatch.setattr(vl, "_download_to", fake_download)

    enc = load_cl100k()
    assert repaired["called"] is True
    assert enc.name == "cl100k_base"
    assert len(enc.encode("hello world")) == 2
    # The repaired file now holds canonical bytes.
    assert _sha256(corrupt) == EXPECTED_SHA256


def test_missing_blob_triggers_redownload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing blob is fetched rather than failing outright."""
    missing = tmp_path / vl.BLOB_FILENAME
    monkeypatch.setattr(vl, "VENDOR_BLOB", missing)
    monkeypatch.setattr(vl, "_VENDOR_DIR", tmp_path)

    def fake_download(target: Path) -> None:
        target.write_bytes(VENDOR_BLOB.read_bytes())

    monkeypatch.setattr(vl, "_download_to", fake_download)

    enc = load_cl100k()
    assert enc.name == "cl100k_base"


def test_download_failure_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """If re-download fails, surface a clear RuntimeError — never silent."""
    corrupt = tmp_path / vl.BLOB_FILENAME
    corrupt.write_bytes(b"bad")
    monkeypatch.setattr(vl, "VENDOR_BLOB", corrupt)
    monkeypatch.setattr(vl, "_VENDOR_DIR", tmp_path)

    def fake_download(target: Path) -> None:
        raise OSError("network down")

    monkeypatch.setattr(vl, "_download_to", fake_download)

    with pytest.raises(RuntimeError, match="re-download failed"):
        load_cl100k()


def test_download_with_bad_checksum_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A re-downloaded blob that STILL fails checksum is rejected loudly."""
    corrupt = tmp_path / vl.BLOB_FILENAME
    corrupt.write_bytes(b"bad")
    monkeypatch.setattr(vl, "VENDOR_BLOB", corrupt)
    monkeypatch.setattr(vl, "_VENDOR_DIR", tmp_path)

    def fake_download(target: Path) -> None:
        target.write_bytes(b"still wrong, not the canonical vocab")

    monkeypatch.setattr(vl, "_download_to", fake_download)

    with pytest.raises(RuntimeError, match="failed its checksum"):
        load_cl100k()


def test_download_to_verifies_tls_against_certifi_cas(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The blob download hands urlopen a certifi-backed SSL context.

    uv-managed pythons ship no system CA store, so a default context fails
    certificate verification. Zero network: urlopen is patched out.
    """
    certifi = pytest.importorskip("certifi")

    class _FakeResponse:
        def read(self, size: int = -1) -> bytes:
            return b""

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

    captured: dict[str, object] = {}

    def fake_urlopen(
        request: object, *, timeout: int, context: ssl.SSLContext
    ) -> _FakeResponse:
        captured["timeout"] = timeout
        captured["context"] = context
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    vl._download_to(tmp_path / vl.BLOB_FILENAME)

    assert captured["timeout"] == vl._DOWNLOAD_TIMEOUT
    context = captured["context"]
    assert isinstance(context, ssl.SSLContext)
    expected = ssl.create_default_context(cafile=certifi.where())
    assert context.get_ca_certs() == expected.get_ca_certs()
