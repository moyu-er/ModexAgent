"""Tokenizer工具"""

import re
from typing import Protocol


class Tokenizer(Protocol):
    """Tokenizer协议"""

    def count(self, text: str) -> int:
        """计算文本的token数"""
        ...


class SimpleTokenizer:
    """
    简单的tokenizer，用于估算token数。
    
    这是一个轻量级的实现，适用于：
    - 快速估算
    - 无需精确计数的场景
    - 避免依赖大型库（如tiktoken）
    
    估算规则：
    - 中文字符：1个token
    - 英文单词：0.75个token
    - 其他字符：0.1个token
    """

    def count(self, text: str) -> int:
        """
        估算文本的token数。
        
        Args:
            text: 输入文本
        
        Returns:
            估算的token数
        """
        if not text:
            return 0

        # 中文字符
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))

        # 英文单词
        english_words = len(re.findall(r'[a-zA-Z]+', text))

        # 其他字符（标点、数字等）
        other_chars = len(text) - chinese_chars - sum(len(w) for w in re.findall(r'[a-zA-Z]+', text))

        # 估算
        return chinese_chars + int(english_words * 0.75) + int(other_chars * 0.1)


class CharTokenizer:
    """
    基于字符数的简单tokenizer。
    
    直接将字符数作为token数，适用于：
    - 简单场景
    - 测试环境
    """

    def count(self, text: str) -> int:
        return len(text)
