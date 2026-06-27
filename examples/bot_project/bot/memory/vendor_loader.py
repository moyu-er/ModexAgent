"""Load the vendored cl100k_base BPE so the bot counts tokens fully offline.

The blob lives at ``vendor/cl100k_base.tiktoken`` in tiktoken's plain BPE
format (``base64(token_bytes) rank`` per line). We load it directly via
``tiktoken.load_tiktoken_bpe`` and build the ``cl100k_base`` Encoding from
its defining constants — no ``TIKTOKEN_CACHE_DIR`` or hash-named file needed,
so the on-disk filename stays human-friendly.

Integrity: the blob is sha256-verified before use. If it is missing or has
been tampered with (or git EOL-converted it), we re-download once from the
public mirror into a writable path and re-verify. A blob that still fails
the check raises — we never silently count tokens with a broken vocabulary.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import urllib.request
from functools import lru_cache
from pathlib import Path

import tiktoken
from tiktoken.load import load_tiktoken_bpe

_VENDOR_DIR = Path(__file__).parent / "vendor"
BLOB_FILENAME = "cl100k_base.tiktoken"
VENDOR_BLOB = _VENDOR_DIR / BLOB_FILENAME

# Authoritative sha256 of the canonical cl100k_base.tiktoken (1,681,126 bytes,
# LF line endings). Pinned so we detect tampering or accidental EOL conversion.
EXPECTED_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"

# Official public mirror. sha1(this url) == 9b5ad71b2ce5302211f9c61530b329a4922fc6a4,
# the historical tiktoken cache filename — confirming provenance.
DOWNLOAD_URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
_DOWNLOAD_TIMEOUT = 60
_USER_AGENT = "modex-bot"

# Defining constants of the cl100k_base encoding. These ARE the spec of
# cl100k_base (they mirror tiktoken_ext.openai_public); building the Encoding
# from an explicit file path is what frees us from the hash-named cache.
_CL100K_PAT_STR = (
    r"(?i:'s|'t|'re|'ve|'m|'ll|'d)"
    r"|[^\r\n\p{L}\p{N}]?\p{L}+"
    r"|\p{N}{1,3}"
    r"| ?[^\s\p{L}\p{N}]+[\r\n]*"
    r"|\s*[\r\n]+"
    r"|\s+(?!\S)"
    r"|\s+"
)
# Full cl100k_base special-token set, mirroring tiktoken's official
# registration. n_vocab derives from max token value (100,276) + 1 = 100,277,
# so we do NOT pass explicit_n_vocab. The fim_* / endofprompt markers are
# included for byte-exact parity with tiktoken.get_encoding("cl100k_base").
_CL100K_SPECIAL_TOKENS = {
    "<|endoftext|>": 100257,
    "<|fim_prefix|>": 100258,
    "<|fim_middle|>": 100259,
    "<|fim_suffix|>": 100260,
    "<|endofprompt|>": 100276,
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_valid(path: Path) -> bool:
    return path.is_file() and _sha256(path) == EXPECTED_SHA256


def _writable_cache_path() -> Path:
    """A user-writable repair location when the installed vendor dir is read-only."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "modex_bot" / "cache"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "modex_bot"
    base.mkdir(parents=True, exist_ok=True)
    return base / BLOB_FILENAME


def _download_to(target: Path) -> None:
    """Download the canonical blob to ``target`` atomically, in binary (no EOL translation)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        with urllib.request.urlopen(
            urllib.request.Request(DOWNLOAD_URL, headers={"User-Agent": _USER_AGENT}),
            timeout=_DOWNLOAD_TIMEOUT,
        ) as resp, open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _resolve_blob() -> Path:
    """Return a path to a checksum-valid blob, repairing/redownloading if needed.

    Fast path: the vendored copy is valid. Otherwise attempt one re-download
    (in place if the vendor dir is writable, else into a user cache dir) and
    re-verify. Raise on any failure — never silently use a broken vocabulary.
    """
    if _is_valid(VENDOR_BLOB):
        return VENDOR_BLOB

    target = VENDOR_BLOB if os.access(_VENDOR_DIR, os.W_OK) else _writable_cache_path()
    try:
        _download_to(target)
    except OSError as exc:
        raise RuntimeError(
            f"cl100k_base blob is missing or corrupt at {VENDOR_BLOB} and "
            f"re-download failed: {exc}. Restore the file from the repo or "
            "enable network access."
        ) from exc

    if not _is_valid(target):
        raise RuntimeError(
            f"Re-downloaded cl100k_base blob at {target} failed its checksum. "
            "Refusing to count tokens with an unverified vocabulary."
        )
    return target


@lru_cache(maxsize=1)
def load_cl100k() -> "tiktoken.Encoding":
    """Build the cl100k_base Encoding from the verified vendored blob (offline)."""
    blob = _resolve_blob()
    ranks = load_tiktoken_bpe(str(blob))
    return tiktoken.Encoding(
        name="cl100k_base",
        pat_str=_CL100K_PAT_STR,
        mergeable_ranks=ranks,
        special_tokens=_CL100K_SPECIAL_TOKENS,
    )
