"""Structural regression test for bundled pool agent descriptions.

Loads every shipped pool under ``examples/bot_project/config/pools`` via the
real :class:`PoolStore` and asserts self-containment invariants on the six
main + subagent specs:

* the discovered agent-name set is exactly the expected six-name roster;
* every description is non-empty;
* no description mentions any OTHER configured agent name as a whole token
  (hyphenated names included) -- a description that names a sibling agent
  exposes topology the model should not see.

The check is purely structural: the agent-name set is derived from the loaded
pools (not a hardcoded forbidden-keyword list), and cross-reference matching
uses regex word boundaries on the exact configured names. The test is
independent of exact prose, sentence fragments, generic words such as
``subagent``/``delegation``/``peer``, or snapshot values.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_BOT_PROJECT = Path(__file__).resolve().parents[3]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from modex_agent.multi_agent.pool_config.store import PoolStore  # noqa: E402

# The complete roster shipped under ``config/pools/``. Drift (a name added or
# removed without updating this set) is itself a regression the test catches.
_EXPECTED_AGENT_NAMES: frozenset[str] = frozenset(
    {"default", "office-expert", "orchestrator", "explore", "coder", "opencode"}
)


def _load_specs(store: PoolStore) -> list[tuple[str, str, str]]:
    """Collect ``(pool_name, agent_name, description)`` for every main + subagent."""
    specs: list[tuple[str, str, str]] = []
    for summary in store.list_pools():
        tree = store.read_pool(summary.name)
        specs.append((summary.name, tree.main.agent_name, tree.main.description))
        for sub in tree.subagents:
            specs.append((summary.name, sub.agent_name, sub.description))
    return specs


_STORE: PoolStore = PoolStore(base_dir=_BOT_PROJECT)
_SPECS: list[tuple[str, str, str]] = _load_specs(_STORE)
_AGENT_NAMES: frozenset[str] = frozenset(name for _, name, _ in _SPECS)
_DESC_BY_AGENT: dict[str, tuple[str, str]] = {name: (pool, desc) for pool, name, desc in _SPECS}

# Ordered (source_agent, other_agent) pairs for the cross-reference check.
_CROSS_PAIRS: list[tuple[str, str]] = [
    (src, other) for src in sorted(_AGENT_NAMES) for other in sorted(_AGENT_NAMES) if other != src
]


def _mentions_whole_token(name: str, text: str) -> bool:
    """Return ``True`` if ``name`` appears in ``text`` as a whole token.

    Hyphenated names (e.g. ``office-expert``) are matched in full: the escaped
    name is wrapped in ``\\b`` word boundaries so surrounding non-word
    characters (spaces, parentheses, punctuation) delimit the match while
    internal hyphens are part of the literal.
    """
    return re.search(rf"\b{re.escape(name)}\b", text) is not None


def test_discovered_agent_names_match_expected_roster() -> None:
    """The six bundled specs must be exactly the expected roster."""
    assert len(_SPECS) == len(_EXPECTED_AGENT_NAMES), (
        f"Expected {len(_EXPECTED_AGENT_NAMES)} specs, found {len(_SPECS)}"
    )
    assert _AGENT_NAMES == _EXPECTED_AGENT_NAMES, (
        f"Discovered names {_AGENT_NAMES} != expected {_EXPECTED_AGENT_NAMES}"
    )


@pytest.mark.parametrize(
    ("pool_name", "agent_name", "description"),
    _SPECS,
    ids=[f"{pool}-{agent}" for pool, agent, _ in _SPECS],
)
def test_description_non_empty(pool_name: str, agent_name: str, description: str) -> None:
    """Every shipped spec must carry a non-empty description."""
    assert description.strip(), (
        f"Agent {agent_name!r} (pool {pool_name!r}) has an empty description"
    )


@pytest.mark.parametrize(
    ("source_agent", "other_agent"),
    _CROSS_PAIRS,
    ids=[f"{src}-names-{other}" for src, other in _CROSS_PAIRS],
)
def test_no_description_mentions_other_agent(source_agent: str, other_agent: str) -> None:
    """No description may name another configured agent as a whole token."""
    pool, desc = _DESC_BY_AGENT[source_agent]
    assert not _mentions_whole_token(other_agent, desc), (
        f"Agent {source_agent!r} (pool {pool!r}) description mentions "
        f"other configured agent name {other_agent!r} as a whole token"
    )
