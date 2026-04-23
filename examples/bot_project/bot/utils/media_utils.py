"""QQ Bot 专用媒体处理工具

提供文件下载功能。通用的文档提取、图片编码等功能已移至
framework.utils.media_utils。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

DEFAULT_DOWNLOAD_CHUNK_SIZE = 256 * 1024  # 256KB
DEFAULT_DOWNLOAD_MAX_BYTES = 200 * 1024 * 1024  # 200MB


def _sanitize_filename(name: str) -> str:
    """清理文件名，防止路径遍历和特殊字符问题。"""
    import re

    _safe_name_re = re.compile(
        r"[^\w.\-()\[\]（）【】\u4e00-\u9fff]+", re.UNICODE
    )
    name = (name or "").strip()
    name = Path(name).name
    name = _safe_name_re.sub("_", name).strip("._ ")
    return name


async def download_file(
    url: str,
    dest_dir: Path,
    filename_hint: str = "",
    max_bytes: int = DEFAULT_DOWNLOAD_MAX_BYTES,
) -> str | None:
    """异步下载文件到指定目录。

    使用 asyncio.to_thread + urllib 实现，不依赖 aiohttp。
    支持流式写入和大小限制。
    """
    url = (url or "").strip()
    if not url:
        return None

    # 处理协议相对 URL
    if url.startswith("//"):
        url = f"https:{url}"

    safe_name = _sanitize_filename(filename_hint)
    ts = int(time.time() * 1000)

    try:

        def _download(safe: str, timestamp: int) -> str | None:
            try:
                with urlopen(url, timeout=120) as resp:  # noqa: S310
                    if resp.status != 200:
                        print(
                            f"[MediaUtils] Download failed: status={resp.status} url={url}"
                        )
                        return None

                    # 推断扩展名
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    ext = Path(urlparse(url).path).suffix
                    if not ext:
                        ext = Path(filename_hint).suffix
                    if not ext:
                        if "png" in ctype:
                            ext = ".png"
                        elif "jpeg" in ctype or "jpg" in ctype:
                            ext = ".jpg"
                        elif "gif" in ctype:
                            ext = ".gif"
                        elif "webp" in ctype:
                            ext = ".webp"
                        elif "pdf" in ctype:
                            ext = ".pdf"
                        else:
                            ext = ".bin"

                    if safe:
                        if not Path(safe).suffix:
                            safe = safe + ext
                        filename = safe
                    else:
                        filename = f"qq_file_{timestamp}{ext}"

                    target = dest_dir / filename
                    if target.exists():
                        target = dest_dir / f"{target.stem}_{timestamp}{target.suffix}"

                    tmp_path = target.with_suffix(target.suffix + ".part")
                    dest_dir.mkdir(parents=True, exist_ok=True)

                    downloaded = 0
                    with open(tmp_path, "wb") as f:
                        while True:
                            chunk = resp.read(DEFAULT_DOWNLOAD_CHUNK_SIZE)
                            if not chunk:
                                break
                            downloaded += len(chunk)
                            if downloaded > max_bytes:
                                print(
                                    f"[MediaUtils] Download exceeded max_bytes={max_bytes}"
                                )
                                f.close()
                                tmp_path.unlink(missing_ok=True)
                                return None
                            f.write(chunk)

                    os.replace(tmp_path, target)
                    print(f"[MediaUtils] File saved: {target}")
                    return str(target)
            except Exception as e:
                print(f"[MediaUtils] Download error: {e}")
                return None

        return await asyncio.to_thread(_download, safe_name, ts)
    except Exception as e:
        print(f"[MediaUtils] Download error: {e}")
        return None
