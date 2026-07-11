from __future__ import annotations

from dataclasses import dataclass, field

from modex_agent.messaging.broker import Address, AddressKind


@dataclass(frozen=True, slots=True)
class AgentAddress(Address):
    """Agent-specific address with role and capability metadata."""

    kind: AddressKind = AddressKind.AGENT
    name: str = ""
    role: str | None = None
    capabilities: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        base = f"{self.kind}:{self.name}"
        if self.role:
            base = f"{base}@{self.role}"
        if self.capabilities:
            caps = ",".join(self.capabilities)
            base = f"{base}[{caps}]"
        return base

    def __hash__(self) -> int:
        # 路由只按 kind+name 匹配，role/capabilities 是元数据不影响投递
        return hash((self.kind, self.name))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Address):
            return NotImplemented
        return self.kind == other.kind and self.name == other.name
