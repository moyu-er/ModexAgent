"""通用工具函数"""

import re
from typing import Optional


def strip_think(text: Optional[str]) -> Optional[str]:
    """去除 <think>...</think> 标签
    
    某些模型（如 DeepSeek）会在回复中嵌入思考过程，
    格式为 <think>思考内容</think>，这个函数用于去除这些标签。
    
    Args:
        text: 原始文本
        
    Returns:
        去除 think 标签后的文本，如果结果为空则返回 None
    """
    if not text:
        return None
    
    # 使用 DOTALL 标志让 . 匹配换行符
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    cleaned = cleaned.strip()
    
    return cleaned if cleaned else None
