"""Shared file-read core for read-type tools.

The pagination/multimodal primitives every file-reading tool converges on
(:class:`~modex_agent.tools.standard.file_tool.ReadFileTool` and the memory
scoped-read tool): paginated text reading with structured metadata, and
image → compress → media-store persist → ``image_url`` reference. Moved out
of ``tools/standard/file_tool.py`` (W2) so the memory domain can reuse the
single implementation instead of reaching up into tools.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from modex_agent.core.capabilities import Modality
from modex_agent.core.media import StoredMediaKind
from modex_agent.core.message import ImageUrl, ImageUrlPart, TextPart, build_media_ref
from modex_agent.core.tool_manager import ToolResult, get_tool_execution_context
from modex_agent.media.media_utils import compress_image

# Internal constants for paginated reading
DEFAULT_READ_LIMIT = 200
MAX_READ_LIMIT = 300
MAX_READ_CHARS = 20_000



def paginate_file(
    file_path: Path,
    offset: int = 0,
    limit: int = DEFAULT_READ_LIMIT,
    max_chars: int = MAX_READ_CHARS,
) -> str:
    """Read a file paginated, returning a result string with structured metadata.

    Args:
        file_path: the already-validated file path
        offset: number of leading lines to skip (0-based)
        limit: maximum number of lines to read (auto-clamped to MAX_READ_LIMIT)
        max_chars: hard ceiling on total characters

    Returns:
        The file content string with a structured suffix, or an error-message
        string.
    """
    # ── Argument validation ──────────────────────────────────
    if offset < 0:
        return f"Error: offset must be >= 0, got {offset}"
    if limit < 1:
        return f"Error: limit must be >= 1, got {limit}"

    # clamp limit
    if limit > MAX_READ_LIMIT:
        limit = MAX_READ_LIMIT

    # ── First pass: count total lines ────────────────────────
    total_lines = 0
    with file_path.open("r", encoding="utf-8") as f:
        for _line in f:
            total_lines += 1

    # ── Empty file ───────────────────────────────────────────
    if total_lines == 0:
        return "(empty file)\n\ntotal_lines: 0\noffset: 0\nread_status: empty"

    # ── offset out of range ──────────────────────────────────
    if offset >= total_lines:
        return (
            f"Error: offset ({offset}) exceeds file length ({total_lines} lines).\n"
            f"File has {total_lines} lines (line numbers 1-{total_lines}).\n"
            f"Valid offset range: 0 ~ {total_lines - 1}.\n"
            f"hint: use offset=0 to read from the beginning"
        )

    # ── Second pass: paginated read ──────────────────────────
    selected_lines: list[str] = []
    accumulated_chars = 0
    char_truncated = False
    last_line_read = offset  # 0-based, index of the last successfully read line

    with file_path.open("r", encoding="utf-8") as f:
        line_idx = 0  # 0-based
        lines_collected = 0

        for raw_line in f:
            # Skip lines before offset
            if line_idx < offset:
                line_idx += 1
                continue

            # limit exhausted → more lines remain
            if lines_collected >= limit:
                break

            line = raw_line.rstrip("\n\r")

            # Soft char ceiling: include the line first, then decide whether to truncate
            selected_lines.append(line)
            accumulated_chars += len(line) + 1  # +1 for newline
            last_line_read = line_idx
            lines_collected += 1
            line_idx += 1

            if accumulated_chars > max_chars:
                char_truncated = True
                break

        # Check whether more lines remain after reading (only when neither
        # the limit nor the char ceiling triggered)
        has_more_by_limit = lines_collected >= limit
        # If not char-truncated, check whether the file has been fully read
        remaining = (
            total_lines - (last_line_read + 1)
            if not char_truncated
            else total_lines - (last_line_read + 1)
        )

    # ── Compute status ───────────────────────────────────────
    actual_start = offset + 1  # 1-based display
    actual_end = last_line_read + 1  # 1-based display

    is_complete = (not char_truncated) and (actual_end == total_lines)
    is_truncated_by_limit = has_more_by_limit and not char_truncated

    # ── Build the result ─────────────────────────────────────
    content = "\n".join(selected_lines)
    parts: list[str] = [content, ""]

    # metadata
    parts.append(f"total_lines: {total_lines}")
    parts.append(f"offset: {offset}")

    if is_complete and not is_truncated_by_limit:
        # Complete read
        if lines_collected < limit:
            parts.append(
                f"read_lines: {actual_start}-{actual_end} (requested {limit}, file has {lines_collected} remaining)"
            )
        else:
            parts.append(f"read_lines: {actual_start}-{actual_end}")
        parts.append("read_status: complete")

    elif is_truncated_by_limit:
        # Truncated by line count
        parts.append(f"read_lines: {actual_start}-{actual_end} (limit reached)")
        parts.append(f"remaining_lines: {remaining}")
        parts.append("read_status: truncated_by_limit")
        parts.append(f"hint: use offset={last_line_read + 1} to read next chunk")

    elif char_truncated:
        # Truncated by char count (returned early before the limit was exhausted)
        parts.append(f"read_lines: {actual_start}-{actual_end} (stopped before limit)")
        parts.append(f"remaining_lines: {remaining}")
        parts.append("read_status: truncated_by_chars")
        parts.append(
            f"warning: char limit ({max_chars}) reached, "
            f"only read {lines_collected} of requested {limit} lines"
        )
        parts.append(f"hint: use offset={last_line_read + 1} to read next chunk")

    return "\n".join(parts)


async def read_image_as_multimodal(
    file_path: Path,
    mime: str,
) -> ToolResult:
    """Read an image file → compress → persist to the media store → reference.

    Capability gate: when the current model lacks ``Modality.IMAGE``, returns
    a brief text result stating the file is an image but visual content is
    not available.  The capability limitation itself is surfaced via the tool
    description (``get_dynamic_schema_for`` adjusts it for text-only models);
    the tool result only states the objective fact — no system diagnosis or
    action advice — so the agent can decide how to proceed.

    When the model is image-capable AND a media store is wired to the tool
    execution context, the compressed bytes are persisted into the READS
    subtree (persist-before-return: the ``media://<aid>`` reference handed
    back is always backed by stored bytes) and the result carries the text
    hint plus an :class:`ImageUrlPart` holding the reference. The reference —
    never a data URL — is what persists into history; the injection layer
    resolves it back to bytes at each LLM call.

    Degradations (always text-only, never a data-URL part): no media store
    wired, or undecodable image bytes.
    """
    ctx = get_tool_execution_context()
    if ctx is None or not ctx.supports(Modality.IMAGE):
        # Tool results are the agent's observations — not a system log channel.
        # The capability limitation is already surfaced via the tool description
        # (get_dynamic_schema_for adjusts it for text-only models).  The result
        # should only state the objective fact and let the agent decide what
        # to do next (skip, ask the user, infer from filename, etc.).  Do NOT put
        # system diagnosis ("model lacks IMAGE capability"), file sizes, or
        # action advice ("use a vision-capable model") here — the agent may
        # have called read autonomously, not at the user's request.
        degradation_text = f"Image file: {file_path} ({mime}). Visual content not available."
        return ToolResult.from_text("read", degradation_text)

    if ctx.media_store is None or ctx.session_id is None:
        return ToolResult.from_text(
            "read",
            f"Image file: {file_path}. Visual content not available (no media store wired).",
        )

    try:
        raw = await asyncio.to_thread(file_path.read_bytes)
        compressed = compress_image(raw, mime)
        if compressed is None:
            return ToolResult.from_text(
                "read",
                f"Image file: {file_path} ({mime}). Visual content not available.",
            )
        aid = uuid4().hex
        ctx.media_store.save(
            ctx.session_id, aid, compressed.data, kind=StoredMediaKind.READS
        )
        text_hint = f"[Image read: {file_path} ({mime})]"
        return ToolResult(
            tool_name="read",
            content=[
                TextPart(text=text_hint),
                ImageUrlPart(image_url=ImageUrl(url=build_media_ref(aid))),
            ],
        )
    except Exception as exc:
        return ToolResult(
            tool_name="read",
            error=f"Failed to read image {file_path.name}: {exc}",
        )
