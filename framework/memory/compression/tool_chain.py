"""Tool-chain aware compression strategy."""

from collections.abc import Sequence
from typing import Any

from framework.memory.core.compression import (
    CompressionContext,
    CompressionResult,
    CompressionStrategy,
)
from framework.core.types import MessageRole
from framework.memory.core.message import ChatMessage
from framework.memory.utils import estimate_token_count


def _is_tool_call(msg: ChatMessage | dict[str, Any]) -> bool:
    if isinstance(msg, ChatMessage):
        return msg.role == MessageRole.ASSISTANT and bool(msg.tool_calls)
    return msg.get("role") == MessageRole.ASSISTANT and bool(msg.get("tool_calls"))


def _is_tool_result(msg: ChatMessage | dict[str, Any]) -> bool:
    if isinstance(msg, ChatMessage):
        return msg.role == MessageRole.TOOL
    return msg.get("role") == MessageRole.TOOL


def _msg_to_dict(msg: ChatMessage | dict[str, Any]) -> dict[str, Any]:
    """将 ChatMessage 或 dict 统一转为 dict。"""
    return msg.to_dict() if isinstance(msg, ChatMessage) else msg


def _find_tool_chain(messages: Sequence[ChatMessage | dict[str, Any]], start_index: int) -> set[int]:
    """给定一个 tool_call 消息索引，返回整个相关 tool 链的所有索引集合。"""
    indices = {start_index}
    if not _is_tool_call(messages[start_index]):
        return indices

    msg = _msg_to_dict(messages[start_index])
    tool_calls = msg.get("tool_calls", [])
    call_ids = {tc.get("id") for tc in tool_calls if tc.get("id")}

    # 向后查找对应的 tool 结果消息
    i = start_index + 1
    while i < len(messages):
        m = _msg_to_dict(messages[i])
        if _is_tool_result(messages[i]) and m.get("tool_call_id") in call_ids:
            indices.add(i)
            i += 1
        else:
            break
    return indices


def _find_safe_truncation_count(
    messages: Sequence[ChatMessage | dict[str, Any]],
    excess: int,
    *,
    protected_count: int = 0,
    min_tail_keep: int = 1,
) -> int:
    """返回一个不截断 tool-call 链的安全截断边界索引。

    如果原始 excess 恰好落在某条 tool 链中间，则将边界扩展到整条链末尾，
    确保 tool_calls 和对应的 tool 结果被同时移除。

    Args:
        messages: 消息列表
        excess: 计划从可删除区域移除的消息数量
        protected_count: 头部受保护的消息数量，不会被移除
        min_tail_keep: 尾部至少要保留的消息数量

    Returns:
        boundary: 截断边界索引（从列表开头计算），调用方应使用
                  messages[protected_count:boundary] 作为实际被裁剪的内容。
    """
    if excess <= 0:
        return 0

    max_boundary = len(messages) - min_tail_keep
    if protected_count >= max_boundary:
        # 保护区 + 强制尾已经超过可删除区域，优雅降级：不删除任何消息
        return protected_count

    boundary = protected_count + excess
    boundary = min(boundary, max_boundary)

    # 收集受保护区内的 tool_call ids，防止误删其对应的 tool result
    protected_call_ids: set[str] = set()
    for idx in range(protected_count):
        if _is_tool_call(messages[idx]):
            msg = _msg_to_dict(messages[idx])
            for tc in msg.get("tool_calls", []):
                if tc.get("id"):
                    protected_call_ids.add(tc.get("id"))

    i = protected_count
    while i < len(messages):
        if _is_tool_call(messages[i]):
            chain = _find_tool_chain(messages, i)
            chain_end = max(chain)
            if i < boundary <= chain_end:
                boundary = chain_end + 1
                if boundary > max_boundary:
                    boundary = i
                    break
            i = chain_end + 1
        else:
            i += 1

    # 如果可删除区域内存在受保护区 tool_call 的 result，则不能删到那里
    if protected_call_ids:
        first_blocked = None
        for idx in range(protected_count, boundary):
            m = _msg_to_dict(messages[idx])
            if _is_tool_result(messages[idx]) and m.get("tool_call_id") in protected_call_ids:
                first_blocked = idx
                break
        if first_blocked is not None:
            boundary = protected_count

    return max(boundary, protected_count)


def _fit_token_window(
    messages: Sequence[ChatMessage | dict[str, Any]],
    max_tokens: int,
    *,
    protected_count: int = 0,
    min_tail_keep: int = 1,
) -> tuple[list[ChatMessage | dict[str, Any]], list[ChatMessage | dict[str, Any]]]:
    """截断消息到 token 窗口内，保持 tool 链完整。

    Args:
        messages: 消息列表
        max_tokens: 最大 token 数
        protected_count: 头部受保护的消息数量，这些消息不会被移除
        min_tail_keep: 尾部至少要保留的消息数量
    """
    dict_messages = [_msg_to_dict(m) for m in messages]
    tokens = estimate_token_count(dict_messages)
    min_keep = max(2, protected_count + min_tail_keep)

    # 优雅降级：如果连保护头 + 强制尾都超出预算或刚好占满，不做截断
    if protected_count + min_tail_keep >= len(messages):
        return list(messages), []

    if tokens <= max_tokens or len(messages) <= min_keep:
        return list(messages), []

    pruned: list[ChatMessage | dict[str, Any]] = []
    working = list(messages)

    # 预计算受保护区内的 tool_call ids
    protected_call_ids: set[str] = set()
    for idx in range(min(protected_count, len(working))):
        if _is_tool_call(working[idx]):
            msg = _msg_to_dict(working[idx])
            for tc in msg.get("tool_calls", []):
                if tc.get("id"):
                    protected_call_ids.add(tc.get("id"))

    # 预建 tool_call_id -> index 映射，避免循环中重复扫描
    call_id_to_index: dict[str, int] = {}
    for idx, m in enumerate(working):
        if _is_tool_call(m):
            msg = _msg_to_dict(m)
            for tc in msg.get("tool_calls", []):
                if tc.get("id"):
                    call_id_to_index[tc["id"]] = idx

    while True:
        dict_working = [_msg_to_dict(m) for m in working]
        current_tokens = estimate_token_count(dict_working)
        if current_tokens <= max_tokens or len(working) <= min_keep:
            break

        removed_idx = protected_count

        # 如果可删除区域已空，停止
        if removed_idx >= len(working) - min_tail_keep:
            break

        # 如果当前位置是受保护 tool_call 的 result，优雅降级：停止截断
        m = _msg_to_dict(working[removed_idx])
        if (
            _is_tool_result(working[removed_idx])
            and m.get("tool_call_id") in protected_call_ids
        ):
            break

        # 如果头部是一个 tool 结果，需要先找到其对应的 tool_call
        if _is_tool_result(working[removed_idx]):
            call_id = m.get("tool_call_id") or ""
            call_idx = call_id_to_index.get(call_id)
            if call_idx is not None:
                chain = _find_tool_chain(working, call_idx)
                for idx in sorted(chain, reverse=True):
                    pruned.insert(0, working.pop(idx))
                # 更新映射：移除被 pop 的索引，并前移后续索引
                call_id_to_index = {
                    cid: (cidx - sum(1 for p in chain if p < cidx))
                    for cid, cidx in call_id_to_index.items()
                    if cidx not in chain
                }
                continue

        # 如果头部是 tool_call，移除整个链
        if _is_tool_call(working[removed_idx]):
            chain = _find_tool_chain(working, removed_idx)
            for idx in sorted(chain, reverse=True):
                pruned.insert(0, working.pop(idx))
            call_id_to_index = {
                cid: (cidx - sum(1 for p in chain if p < cidx))
                for cid, cidx in call_id_to_index.items()
                if cidx not in chain
            }
            continue

        # 普通消息直接移除
        pruned.insert(0, working.pop(removed_idx))
        call_id_to_index = {
            cid: (cidx - 1 if cidx > removed_idx else cidx)
            for cid, cidx in call_id_to_index.items()
        }

    return working, pruned


class ToolChainAwareStrategy(CompressionStrategy):
    """Tool 链感知压缩策略。

    确保 assistant 的 `tool_calls` 和对应的 `tool` 结果消息始终成对被保留或移除，
    不会出现只保留工具结果但丢失了工具调用请求的情况。
    """

    def __init__(self, max_tokens: int = 4000):
        self.max_tokens = max_tokens

    async def compress(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        context: CompressionContext,
    ) -> CompressionResult:
        target = context.target_token_count or self.max_tokens
        remaining, pruned = _fit_token_window(messages, target)
        if not pruned:
            return CompressionResult(summary="", pruned_messages=[], remaining_messages=list(messages))

        return CompressionResult(
            summary="",
            pruned_messages=pruned,
            remaining_messages=remaining,
            importance=0.5,
        )
