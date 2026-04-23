"""Multi-agent 通用工具函数。"""


POOL_SESSION_SEPARATOR: str = ":"


def parse_pool_session_id(session_id: str) -> tuple[str, str | None]:
    """解析 Pool 内部 session_id 为 (conversation_id, agent_name)。

    Pool 内部使用 {conversation_id}:{agent_name} 作为 session_id。
    如果 session_id 不包含分隔符，则返回 (session_id, None)。
    """
    if POOL_SESSION_SEPARATOR in session_id:
        conversation_id, agent_name = session_id.rsplit(POOL_SESSION_SEPARATOR, 1)
        return conversation_id, agent_name
    return session_id, None


def format_pool_session_id(conversation_id: str, agent_name: str) -> str:
    """构造 Pool 内部 session_id（两段格式，用于 user↔main 主会话）。

    Args:
        conversation_id: 外部会话标识（如 user_openid）
        agent_name: Agent 名称

    Returns:
        {conversation_id}:{agent_name}
    """
    return f"{conversation_id}{POOL_SESSION_SEPARATOR}{agent_name}"


def format_peer_session_id(conversation_id: str, sender_agent: str, receiver_agent: str) -> str:
    """构造 Peer Pair  session_id（三段格式，用于 Agent 间通信）。

    Args:
        conversation_id: 外部会话标识
        sender_agent: 发送方 Agent 名称
        receiver_agent: 接收方 Agent 名称

    Returns:
        {conversation_id}:{sender_agent}:{receiver_agent}
    """
    return f"{conversation_id}{POOL_SESSION_SEPARATOR}{sender_agent}{POOL_SESSION_SEPARATOR}{receiver_agent}"


def parse_peer_session_id(session_id: str) -> tuple[str, str | None, str | None]:
    """解析 Peer Pair session_id 为 (conversation_id, sender_agent, receiver_agent)。

    只处理三段格式；如果不是三段，返回 (session_id, None, None)。
    """
    parts = session_id.split(POOL_SESSION_SEPARATOR)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return session_id, None, None


def is_peer_session_id(session_id: str) -> bool:
    """判断 session_id 是否为三段格式的 peer session。

    Args:
        session_id: 待判断的 session_id。

    Returns:
        True 如果是三段格式（含两个分隔符），否则 False。
    """
    return session_id.count(POOL_SESSION_SEPARATOR) == 2


def reverse_peer_session_id(session_id: str) -> str:
    """反转 peer session_id 的 sender 和 receiver。

    {conversation_id}:{sender}:{receiver} -> {conversation_id}:{receiver}:{sender}

    如果不是三段格式，原样返回。

    Args:
        session_id: Peer session_id。

    Returns:
        反转后的 session_id（receiver 和 sender 互换）。
    """
    parts = session_id.split(POOL_SESSION_SEPARATOR)
    if len(parts) == 3:
        return f"{parts[0]}{POOL_SESSION_SEPARATOR}{parts[2]}{POOL_SESSION_SEPARATOR}{parts[1]}"
    return session_id
