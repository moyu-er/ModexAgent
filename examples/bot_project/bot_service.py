"""Bot service entry point.

QQ Bot specialization of the generic BotService.
Run with: python bot_service.py

Uses IOC AppConfig.from_yaml() for config loading. The BotService
accepts app_config= for IOC-aware initialization paths.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Literal

# Path setup
framework_dir = Path(__file__).parent.parent.parent
if str(framework_dir) not in sys.path:
    sys.path.insert(0, str(framework_dir))

from bot.logging import setup_logging  # noqa: E402

setup_logging()

from bot.adapters.qq import (  # noqa: E402
    QQBotEmitter,
    QQEmitterConfig,
    QQInputAdapter,
    QQOutputAdapter,
)
from bot.service import BotService  # noqa: E402
from bot.utils.config_loader import ConfigLoader  # noqa: E402

from framework.ioc.configs.app import AppConfig  # noqa: E402
from framework.pipeline.adapters import SessionPrefixStripAdapter  # noqa: E402


class QQBotService(BotService):
    """QQ Bot service -- specialization of the generic BotService.

    Config is loaded via IOC AppConfig.from_yaml() and passed as
    both app_config= (for IOC-aware paths) and config= (for legacy
    dict paths). The legacy bridge has been removed.
    """

    def __init__(self, config_dir: Path, mode: Literal["pipeline", "pool"] = "pipeline") -> None:
        yaml_path = config_dir / "bot_config.yml"

        # Load via IOC layer
        ioc_cfg = AppConfig.from_yaml(yaml_path)
        print(f"[IOC] Loaded config: {len(ioc_cfg.agents)} agents, "
              f"MCP={ioc_cfg.mcp is not None}")

        # QQ-specific config (adapter, not framework)
        config_loader = ConfigLoader(config_dir)
        raw_config = config_loader.load_yaml("bot_config.yml")
        qq_config = raw_config.get("qq", {})
        mcp_config = config_loader.load_mcp_config(raw_config.get("mcp", {}))

        # Build legacy dict from IOC (minimal — just what existing code needs)
        legacy_cfg = {
            "llm": ioc_cfg.llm.model_dump(),
            "agent": {
                "system_prompt": ioc_cfg.agents[0].system_prompt if ioc_cfg.agents else "",
                "max_iterations": ioc_cfg.agents[0].max_steps if ioc_cfg.agents else 20,
                "approval": (
                    ioc_cfg.agents[0].approval.model_dump()
                    if ioc_cfg.agents and ioc_cfg.agents[0].approval
                    else {"enabled": False, "tools": {}}
                ),
            },
            "multi_agent": {
                "enabled": len(ioc_cfg.agents) > 1,
                "parent_agent_name": ioc_cfg.agents[0].name if ioc_cfg.agents else "main",
                "peers": [
                    {"name": a.name, "system_prompt": a.system_prompt}
                    for a in ioc_cfg.agents[1:]
                ],
            },
            "memory": (
                _ioc_memory_to_dict(ioc_cfg.agents[0].memory) if ioc_cfg.agents else {}
            ),
            "mcp": mcp_config,
            "tools": {"file_tools": {"enabled": True}, "shell_tools": {"enabled": True}, "search_tools": {"enabled": True}},
            "paths": ioc_cfg.paths.model_dump(),
        }

        input_adapter = QQInputAdapter(
            app_id=qq_config["app_id"],
            secret=qq_config["secret"],
            sandbox=qq_config.get("sandbox", False),
            allow_from=qq_config.get("allow_from", ["*"]),
            media_dir=qq_config.get("media_dir"),
        )
        qq_output_adapter = QQOutputAdapter(input_adapter)
        output_adapter = SessionPrefixStripAdapter(qq_output_adapter)

        def emitter_factory(session_id: str) -> QQBotEmitter:
            return QQBotEmitter(
                output_adapter=qq_output_adapter,
                session_id=session_id,
                config=QQEmitterConfig.minimal(),
            )

        super().__init__(config_dir, input_adapter, output_adapter, emitter_factory,
                         mode=mode, config=legacy_cfg, app_config=ioc_cfg)


def _ioc_memory_to_dict(m: object | None) -> dict:
    """Minimal IOC Memory → legacy dict converter."""
    if m is None:
        return {"main": {"short_term": {"max_messages": 100}}}
    from framework.ioc.configs.memory import MemoryConfig
    if not isinstance(m, MemoryConfig):
        return {"main": {"short_term": {"max_messages": 100}}}
    return {
        "main": {
            "short_term": {
                "max_messages": m.short_term.max_messages,
                "max_tokens": m.short_term.max_tokens,
                "keep_ratio_for_messages": m.short_term.keep_ratio,
                "keep_ratio_for_token": m.short_term.keep_ratio,
                "auto_llm_compression": m.short_term.auto_llm_compression,
            },
            "long_term": {"enabled": m.long_term is not None and m.long_term.enabled},
            "dream_engine": {"enabled": m.dream_engine is not None and m.dream_engine.enabled},
            "governance": _governance_to_dict(m.governance),
        },
        "peers": {"short_term": {"max_messages": 50}},
        "subagents": {"short_term": {"max_messages": 50}},
    }


def _governance_to_dict(g: object | None) -> dict:
    if g is None:
        return {"enabled": False}
    from framework.ioc.configs.memory import GovernanceConfig
    if not isinstance(g, GovernanceConfig):
        return {"enabled": False}
    return {
        "enabled": True,
        "tool_chain_repair": g.tool_chain_repair,
        "token_budget": (
            {"enabled": True, "budget_ratio": g.token_budget.budget_ratio,
             "safety_buffer": g.token_budget.safety_buffer}
            if g.token_budget else {"enabled": False}
        ),
        "lossy_compaction": (
            {"enabled": True, "tool_result_head_chars": g.lossy_compaction.tool_result_head_chars,
             "assistant_head_chars": g.lossy_compaction.assistant_head_chars}
            if g.lossy_compaction else {"enabled": False}
        ),
    }


def create_qq_service(
    config_dir: Path, mode: Literal["pipeline", "pool"] = "pipeline"
) -> QQBotService:
    """Create a QQ Bot service instance."""
    return QQBotService(config_dir, mode=mode)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the bot service."""
    parser = argparse.ArgumentParser(description="Run the QQ Bot example service.")
    parser.add_argument(
        "--mode",
        choices=("pipeline", "pool"),
        default="pool",
        help="Runtime mode: pipeline for single-agent mode, pool for AgentPool mode.",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> None:
    """Main entry point."""
    args = parse_args(argv)
    config_dir = Path(__file__).parent / "config"
    service = create_qq_service(config_dir, mode=args.mode)
    await service.initialize()
    await service.start()


if __name__ == "__main__":
    asyncio.run(main())
