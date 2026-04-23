from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import yaml

from framework.multi_agent.descriptor import AgentDescriptor, AgentLLMConfig

from .address import AgentAddress

logger = logging.getLogger(__name__)


class AgentDiscovery(ABC):
    """基于约定的 Agent 发现协议。

    实现此协议的类可以从目录、数据库、配置中心或其他来源扫描并生成 AgentDescriptor。
    """

    @abstractmethod
    async def discover(self) -> list[AgentDescriptor]:
        """发现并返回所有可用的 Agent 描述符。"""
        ...


class FileAgentDiscovery(AgentDiscovery):
    """从文件系统目录约定中扫描 Agent 配置（AGENT.yaml）。"""

    def __init__(
        self,
        agents_dir: Path,
        defaults: dict[str, Any] | None = None,
        filename: str = "AGENT.yaml",
    ) -> None:
        self._agents_dir = Path(agents_dir).expanduser().resolve()
        self._defaults = defaults or {}
        self._filename = filename

    async def discover(self) -> list[AgentDescriptor]:
        descriptors: list[AgentDescriptor] = []
        if not self._agents_dir.exists():
            logger.warning("Agents directory does not exist: %s", self._agents_dir)
            return descriptors

        for subdir in sorted(self._agents_dir.iterdir()):
            if not subdir.is_dir():
                continue
            config_path = subdir / self._filename
            if not config_path.exists():
                continue
            try:
                descriptor = self._load_descriptor(config_path)
                if descriptor is not None:
                    descriptors.append(descriptor)
            except Exception as exc:
                logger.warning("Failed to load agent config from %s: %s", config_path, exc)
        return descriptors

    def _load_descriptor(self, path: Path) -> AgentDescriptor | None:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        merged = dict(self._defaults)
        merged.update(data)

        name = merged.get("name") or path.parent.name
        address = AgentAddress(
            kind="agent",
            name=name,
            role=merged.get("role"),
            capabilities=merged.get("capabilities", []),
        )

        llm = merged.get("llm_config", {})
        llm_config = AgentLLMConfig(
            model=llm.get("model"),
            temperature=llm.get("temperature", 0.7),
            max_tokens=llm.get("max_tokens"),
            top_p=llm.get("top_p", 1.0),
            reasoning_effort=llm.get("reasoning_effort"),
            extra_params=llm.get("extra_params", {}),
        )

        return AgentDescriptor(
            address=address,
            llm_config=llm_config,
            system_prompt_template=merged.get("system_prompt", ""),
            allowed_tools=merged.get("allowed_tools"),
            denied_tools=merged.get("denied_tools"),
            allowed_skills=merged.get("allowed_skills"),
            max_iterations=merged.get("max_iterations", 15),
            max_tools_per_turn=merged.get("max_tools_per_turn", 10),
            context_strategy=merged.get("context_strategy", "persistent"),
            role_description=merged.get("role_description", ""),
            specialties=merged.get("specialties", []),
            exposed_to_peers=merged.get("exposed_to_peers", True),
        )
