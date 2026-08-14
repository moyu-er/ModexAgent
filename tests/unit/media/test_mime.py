"""Tests for media.mime — magic-byte sniffing + kind classification."""

from __future__ import annotations

from pathlib import Path

from modex_agent.media.mime import classify_kind, sniff_mime
from modex_agent.media.models import Kind


class TestSniffMimeMagicBytes:
    def test_png_magic(self) -> None:
        assert sniff_mime(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "photo.png") == "image/png"

    def test_jpeg_magic(self) -> None:
        assert sniff_mime(b"\xff\xd8\xff\xe0" + b"\x00" * 32, "photo.jpg") == "image/jpeg"

    def test_gif87a_magic(self) -> None:
        assert sniff_mime(b"GIF87a" + b"\x00" * 32, "anim.gif") == "image/gif"

    def test_gif89a_magic(self) -> None:
        assert sniff_mime(b"GIF89a" + b"\x00" * 32, "anim.gif") == "image/gif"

    def test_webp_magic(self) -> None:
        # RIFF <4 size bytes> WEBP
        body = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32
        assert sniff_mime(body, "pic.webp") == "image/webp"

    def test_riff_but_not_webp_is_none_by_magic(self) -> None:
        # RIFF container that is not WEBP (e.g. WAV) must not be misclassified
        # as webp by the WEBP-tag check. With no filename supplied, the sniffer
        # has no extension fallback and must return None — the WEBP tag at
        # offset 8 is the only RIFF-based magic, and "WAVE" is not it.
        body = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 32
        assert sniff_mime(body, None) is None

    def test_misnamed_image_detected_by_magic(self) -> None:
        # File named .txt but bytes are PNG.
        assert sniff_mime(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "trick.txt") == "image/png"

    def test_extension_fallback_when_no_known_magic(self) -> None:
        # A binary blob with no known magic and no recognizable extension → None.
        assert sniff_mime(b"\x00\x01\x02\x03garbage", "blob.unknownext") is None

    def test_pdf_detected_by_magic_regardless_of_extension(self) -> None:
        # PDF magic (%PDF-) is authoritative: a PDF stream with a .txt or
        # stripped extension must still be detected as application/pdf.
        assert sniff_mime(b"%PDF-1.4\n" + b"\x00" * 32, "report.txt") == "application/pdf"
        assert sniff_mime(b"%PDF-1.7\n" + b"\x00" * 32, None) == "application/pdf"

    def test_unknown_binary_returns_none_without_extension(self) -> None:
        assert sniff_mime(b"\x00\x01\x02\x03garbage", "blob.dat") is None


class TestClassifyKind:
    def test_image_png(self) -> None:
        assert classify_kind("image/png") is Kind.IMAGE

    def test_image_jpeg(self) -> None:
        assert classify_kind("image/jpeg") is Kind.IMAGE

    def test_image_gif(self) -> None:
        assert classify_kind("image/gif") is Kind.IMAGE

    def test_image_webp(self) -> None:
        assert classify_kind("image/webp") is Kind.IMAGE

    def test_pdf_is_extractable(self) -> None:
        assert classify_kind("application/pdf") is Kind.EXTRACTABLE_DOCUMENT

    def test_docx_is_extractable(self) -> None:
        assert (
            classify_kind(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
            is Kind.EXTRACTABLE_DOCUMENT
        )

    def test_xlsx_is_extractable(self) -> None:
        assert (
            classify_kind(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            is Kind.EXTRACTABLE_DOCUMENT
        )

    def test_pptx_is_extractable(self) -> None:
        assert (
            classify_kind(
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            )
            is Kind.EXTRACTABLE_DOCUMENT
        )

    def test_plain_text_is_extractable(self) -> None:
        assert classify_kind("text/plain") is Kind.EXTRACTABLE_DOCUMENT

    def test_csv_is_extractable(self) -> None:
        assert classify_kind("text/csv") is Kind.EXTRACTABLE_DOCUMENT

    def test_markdown_is_extractable(self) -> None:
        assert classify_kind("text/markdown") is Kind.EXTRACTABLE_DOCUMENT

    def test_unknown_binary_is_other(self) -> None:
        assert classify_kind("application/octet-stream") is Kind.OTHER

    def test_unknown_mime_is_other(self) -> None:
        assert classify_kind("application/x-something-weird") is Kind.OTHER

    def test_none_mime_is_other(self) -> None:
        assert classify_kind(None) is Kind.OTHER


class TestMisnamedFileIntegration:
    """The headline case from the task: misnamed image classified by magic."""

    def test_misnamed_png_classifies_as_image(self, tmp_path: Path) -> None:
        fp = tmp_path / "report.txt"
        fp.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        data = fp.read_bytes()
        mime = sniff_mime(data[:64], fp.name)
        assert classify_kind(mime) is Kind.IMAGE

    def test_unknown_binary_classifies_as_other(self, tmp_path: Path) -> None:
        fp = tmp_path / "blob.dat"
        fp.write_bytes(b"\x00\x01\x02\x03" + b"noise" * 20)
        data = fp.read_bytes()
        mime = sniff_mime(data[:64], fp.name)
        assert classify_kind(mime) is Kind.OTHER

    def test_txt_classifies_as_extractable(self, tmp_path: Path) -> None:
        fp = tmp_path / "notes.txt"
        fp.write_text("hello world", encoding="utf-8")
        data = fp.read_bytes()
        mime = sniff_mime(data[:64], fp.name)
        assert classify_kind(mime) is Kind.EXTRACTABLE_DOCUMENT
