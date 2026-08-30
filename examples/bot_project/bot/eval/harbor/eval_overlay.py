"""Typed loader for eval-arm overlays over the framework scope declaration."""

from __future__ import annotations

from collections.abc import Collection
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from modex_agent.scope import AgentOverlay, PoolOverlay, ScopeOverlay
from modex_agent.scope.spec import MemoryDeclaration
from modex_agent.tools.presets import ToolPreset

_TARGET_POOL_KEY = "target_pool"


class EvalArmName(StrEnum):
    DEFAULT = "default"
    BENCHMARK = "benchmark"


class EvalAgentOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    toolset: ToolPreset | None = None
    tools: list[str] | None = None
    memory: MemoryDeclaration | None = None
    system_prompt_provider: str | None = None
    # Open heterogeneous payload mirroring AgentSpec; the named prompt factory
    # validates its config model during assembly.
    system_prompt_provider_config: dict[str, Any] = Field(default_factory=dict)
    strip_approval: bool = False
    strip_mcp: bool = False

    def to_scope_overlay(self) -> AgentOverlay:
        return AgentOverlay.model_validate(self.model_dump(exclude_unset=True))


class EvalSystemPromptOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    path: str


class EvalPoolOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    keep_agents: list[str] | None = None
    agents: dict[str, EvalAgentOverlay] = Field(default_factory=dict)
    single_agent: bool = False
    tools_remove: list[str] = Field(default_factory=list)
    memory: MemoryDeclaration | None = None
    system_prompt: EvalSystemPromptOverlay | None = None
    strip_mcp: bool = False

    def to_scope_overlay(self, root_agent_name: str) -> PoolOverlay:
        """Expand pool sugar into the corresponding framework overlay fields.

        ``root_agent_name`` comes from the already-loaded scope declaration;
        the eval file does not duplicate declaration identity.
        """
        if self.single_agent and self.keep_agents is not None:
            msg = "single_agent and keep_agents are two spellings of the same field"
            raise ValueError(msg)

        agents = {name: overlay.to_scope_overlay() for name, overlay in self.agents.items()}
        root_overlay = agents.get(root_agent_name, AgentOverlay())
        root_updates: dict[str, list[str] | MemoryDeclaration | str | dict[str, str] | bool] = {}
        if self.tools_remove:
            if root_overlay.tools is not None:
                msg = (
                    "tools_remove and root agents.<name>.tools are two spellings of the same field"
                )
                raise ValueError(msg)
            root_updates["tools"] = [f"-{name}" for name in self.tools_remove]
        if self.memory is not None:
            if root_overlay.memory is not None:
                msg = "memory and root agents.<name>.memory are two spellings of the same field"
                raise ValueError(msg)
            root_updates["memory"] = self.memory
        if self.system_prompt is not None:
            if (
                root_overlay.system_prompt_provider is not None
                or "system_prompt_provider_config" in root_overlay.model_fields_set
            ):
                msg = (
                    "system_prompt and root agents.<name> prompt fields are two spellings "
                    "of the same field"
                )
                raise ValueError(msg)
            root_updates["system_prompt_provider"] = self.system_prompt.provider
            root_updates["system_prompt_provider_config"] = {"path": self.system_prompt.path}
        if self.strip_mcp:
            if root_overlay.strip_mcp:
                msg = "strip_mcp and root agents.<name>.strip_mcp are two spellings of the same field"
                raise ValueError(msg)
            root_updates["strip_mcp"] = True
        if root_updates:
            agents[root_agent_name] = root_overlay.model_copy(update=root_updates)

        keep_agents = [root_agent_name] if self.single_agent else self.keep_agents
        return PoolOverlay(keep_agents=keep_agents, agents=agents)


class EvalArmOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strip_peers: bool = False
    pools: dict[str, EvalPoolOverlay] = Field(default_factory=dict)

    def to_scope_overlay(
        self,
        target_pool: str,
        root_agent_name: str,
        registered_tool_names: Collection[str],
    ) -> ScopeOverlay:
        requested_removals = {name for pool in self.pools.values() for name in pool.tools_remove}
        unknown_removals = requested_removals - set(registered_tool_names)
        if unknown_removals:
            msg = f"tools_remove references unregistered tool(s): {sorted(unknown_removals)}"
            raise ValueError(msg)

        converted: dict[str, PoolOverlay] = {}
        for pool_name, pool in self.pools.items():
            resolved_pool_name = target_pool if pool_name == _TARGET_POOL_KEY else pool_name
            uses_sugar = (
                pool.single_agent
                or bool(pool.tools_remove)
                or pool.memory is not None
                or pool.system_prompt is not None
                or pool.strip_mcp
            )
            if uses_sugar and resolved_pool_name != target_pool:
                msg = (
                    "eval pool sugar requires the selected target pool; "
                    f"found sugar under {pool_name!r}, target is {target_pool!r}"
                )
                raise ValueError(msg)
            converted[resolved_pool_name] = pool.to_scope_overlay(root_agent_name)
        return ScopeOverlay(strip_peers=self.strip_peers, pools=converted)


class EvalOverlayFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    arms: dict[EvalArmName, EvalArmOverlay]


def load_eval_arm(path: Path, arm_name: str) -> EvalArmOverlay:
    config = EvalOverlayFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    try:
        selected = EvalArmName(arm_name)
    except ValueError:
        msg = f"unknown eval arm {arm_name!r}"
        raise ValueError(msg) from None
    try:
        return config.arms[selected]
    except KeyError:
        msg = f"eval overlay file {path} does not define arm {selected.value!r}"
        raise ValueError(msg) from None
