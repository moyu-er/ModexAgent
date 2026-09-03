"""Shared capability-supply construction and lifecycle."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType

from modex_agent.plugins.assembly.spec import AssemblySpec
from modex_agent.plugins.capability import (
    CapabilitySupply,
    PoolSupplyAgentEntry,
    PoolSupplyView,
)
from modex_agent.plugins.registry import ComponentRegistry

logger = logging.getLogger(__name__)


async def assemble_capability_supplies(
    specs: tuple[AssemblySpec, ...],
    registry: ComponentRegistry,
    view: PoolSupplyView,
) -> tuple[Mapping[str, CapabilitySupply], tuple[CapabilitySupply, ...]]:
    """Construct and start supplies atomically in deterministic declaration order.

    Returns the immutable consumer mapping and ordered lifecycle handles. Any
    construction or start failure stops every constructed product in reverse
    order before the original failure propagates.
    """
    entries_by_name: dict[str, list[PoolSupplyAgentEntry]] = {}
    for spec in specs:
        for compiled in spec.capabilities:
            entries_by_name.setdefault(compiled.name, []).append(
                PoolSupplyAgentEntry(agent_name=spec.agent_name, config=compiled.config)
            )

    supply: dict[str, CapabilitySupply] = {}
    products: list[CapabilitySupply] = []
    try:
        for name, entries in entries_by_name.items():
            capability = registry.resolve_capability(name)
            product = capability.supply(
                view.model_copy(update={"entries": tuple(entries)})
            )
            if product is not None:
                supply[name] = product
                products.append(product)
        supplies = tuple(products)
        for product in supplies:
            await product.start()
    except BaseException:
        await stop_capability_supplies(tuple(products))
        raise
    return MappingProxyType(supply), supplies


async def stop_capability_supplies(
    supplies: tuple[CapabilitySupply, ...],
) -> None:
    """Stop supplies in reverse order with per-supply exception isolation."""
    for supply in reversed(supplies):
        try:
            await supply.stop()
        except Exception:
            logger.warning(
                "Capability supply %s stop failed; continuing with remaining stops",
                type(supply).__name__,
                exc_info=True,
            )
