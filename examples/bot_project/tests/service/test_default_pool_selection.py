"""PA-07: unified default-pool selection for NEW conversations.

Pure selection rule + the S5 routing integration, exercised over real temp
config roots and the real routing store. The selection function owns exactly
one concern: given (configured preferred pool, declared/configured pool set,
runtime-available pool set), which pool should a NEW choice default to?
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bot.config.domains.personal_assistant import (
    PersonalAssistantPreferences,
)
from bot.service.default_pool_selection import (
    DefaultPoolDecision,
    DefaultPoolStatus,
    resolve_default_pool,
)
from pydantic import ValidationError

from modex_agent.input_pipeline.envelope import UserInputEnvelope
from modex_agent.multi_agent.pool_router import LocalFilePoolRoutingStore

# ── Pure selection rule ─────────────────────────────────────────────────────


def test_unconfigured_preference_never_picks_a_pool() -> None:
    """DESIGN §3.3: with no valid preference there is NO product default —
    never the first declared pool, never main. The caller must prompt."""
    decision = resolve_default_pool(
        preferred=None, declared_pools=["default", "coder"], runtime_pools={"default", "coder"}
    )
    assert decision.pool is None
    assert decision.status is DefaultPoolStatus.NONE_CONFIGURED
    assert decision.reason


def test_valid_preferred_pool_selected_when_declared_and_running() -> None:
    decision = resolve_default_pool(
        preferred="coder",
        declared_pools=["default", "coder", "review"],
        runtime_pools={"default", "coder", "review"},
    )
    assert decision.pool == "coder"
    assert decision.status is DefaultPoolStatus.PREFERRED


def test_preferred_pool_declared_but_not_running_reports_unavailable() -> None:
    """Configured vs runtime split: a declared-but-not-running preferred pool
    (external CLI missing, not yet restarted) must NOT silently select another
    pool — the caller must prompt for selection."""
    decision = resolve_default_pool(
        preferred="coder", declared_pools=["default", "coder"], runtime_pools={"default"}
    )
    assert decision.pool is None
    assert decision.status is DefaultPoolStatus.PREFERRED_UNAVAILABLE
    assert "coder" in decision.reason


def test_preferred_pool_undeclared_reports_unavailable() -> None:
    """Deleted preferred pool (still saved in prefs): explicit unavailable."""
    decision = resolve_default_pool(
        preferred="ghost", declared_pools=["default"], runtime_pools={"default"}
    )
    assert decision.pool is None
    assert decision.status is DefaultPoolStatus.PREFERRED_UNAVAILABLE
    assert "ghost" in decision.reason


def test_no_declared_pools_reports_none_configured() -> None:
    decision = resolve_default_pool(
        preferred="default", declared_pools=[], runtime_pools=set()
    )
    assert decision.pool is None
    assert decision.status is DefaultPoolStatus.PREFERRED_UNAVAILABLE


def test_runtime_only_pool_never_satisfies_preference() -> None:
    """A preferred pool existing only at runtime (stale runtime, declaration
    edited) is NOT declared — the configured truth refuses it."""
    decision = resolve_default_pool(
        preferred="ghost", declared_pools=["default"], runtime_pools={"default", "ghost"}
    )
    assert decision.pool is None
    assert decision.status is DefaultPoolStatus.PREFERRED_UNAVAILABLE


def test_pristine_default_preference_behaves_like_any_preference() -> None:
    """The shipped pristine value 'default' is a preference, not a fallback:
    it wins when available and is reported unavailable when not."""
    decision = resolve_default_pool(
        preferred="default", declared_pools=["default", "coder"], runtime_pools={"default", "coder"}
    )
    assert decision.pool == "default"
    assert decision.status is DefaultPoolStatus.PREFERRED
    missing = resolve_default_pool(
        preferred="default", declared_pools=["default", "coder"], runtime_pools={"coder"}
    )
    assert missing.pool is None
    assert missing.status is DefaultPoolStatus.PREFERRED_UNAVAILABLE


def test_decision_shape_is_frozen() -> None:
    decision = DefaultPoolDecision(
        pool="default", status=DefaultPoolStatus.PREFERRED, reason=""
    )
    with pytest.raises(ValidationError):
        decision.pool = "x"  # type: ignore[misc]


# ── Integration: preferences → selection over temp roots ────────────────────


def test_selection_reads_saved_preference_from_disk(tmp_path: Path) -> None:
    prefs = PersonalAssistantPreferences(tmp_path / "personal_assistant.yml")
    decision = resolve_default_pool(
        preferred=prefs.preferred_pool(),
        declared_pools=["default", "coder"],
        runtime_pools={"default", "coder"},
    )
    assert decision.pool == "default"  # pristine preference default
    prefs.save(default_workspace=None, default_pool="coder")
    decision = resolve_default_pool(
        preferred=prefs.preferred_pool(),
        declared_pools=["default", "coder"],
        runtime_pools={"default", "coder"},
    )
    assert decision.pool == "coder"


def test_unreadable_preference_yields_none_not_fallback(tmp_path: Path) -> None:
    (tmp_path / "personal_assistant.yml").write_text(
        "default_pool: [broken\n", encoding="utf-8"
    )
    prefs = PersonalAssistantPreferences(tmp_path / "personal_assistant.yml")
    assert prefs.preferred_pool() is None
    decision = resolve_default_pool(
        preferred=None, declared_pools=["default"], runtime_pools={"default"}
    )
    # preference unreadable => NO default: the caller surfaces the config
    # error and asks for an explicit choice (never first-pool fallback)
    assert decision.pool is None
    assert decision.status is DefaultPoolStatus.NONE_CONFIGURED


# ── Routing integration: explicit choice pins, preference does not rewrite ──


def _envelope(explicit: str | None) -> UserInputEnvelope:
    return UserInputEnvelope(
        external_id="u1",
        content="hi",
        channel="qq",
        explicit_pool=explicit,
    )


def test_stored_route_unchanged_when_preference_changes(tmp_path: Path) -> None:
    """V12 core: changing the default affects only NEW choices; the stored
    route (historical session attribution) is untouched."""
    store = LocalFilePoolRoutingStore(tmp_path / "routing")
    store.set_pool("convA", "coder")

    prefs = PersonalAssistantPreferences(tmp_path / "personal_assistant.yml")
    prefs.save(default_workspace=None, default_pool="review")

    # stored route wins over any preference
    assert store.get_pool("convA") == "coder"
    # a NEW conversation would get the preference
    decision = resolve_default_pool(
        preferred=prefs.preferred_pool(),
        declared_pools=["default", "coder", "review"],
        runtime_pools={"default", "coder", "review"},
    )
    assert decision.pool == "review"


@pytest.mark.asyncio
async def test_im_first_message_pins_preference_through_s5(tmp_path: Path) -> None:
    """First IM message with no explicit pool and no stored route resolves via
    the preferred default AND pins the route (through the existing routing
    owner — no alternate route state)."""
    from unittest.mock import MagicMock

    from bot.input_pipeline.context import BotInputContext
    from bot.input_pipeline.stages.resolve_pool import ResolvePoolStage, RoutingMeta

    store = LocalFilePoolRoutingStore(tmp_path / "routing")
    prefs = PersonalAssistantPreferences(tmp_path / "personal_assistant.yml")
    prefs.save(default_workspace=None, default_pool="coder")
    decision = resolve_default_pool(
        preferred=prefs.preferred_pool(),
        declared_pools=["default", "coder"],
        runtime_pools={"default", "coder"},
    )

    ctx = BotInputContext(
        default_pool=decision.pool,
        available_pools=lambda: {"default", "coder"},
        pool_session_store=store,
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
    )
    env = _envelope(explicit=None)

    from modex_agent.core.session_id import encode_snowflake

    prefix = encode_snowflake("u1")
    await ResolvePoolStage().process(env, ctx)
    assert env.metadata[RoutingMeta.RESOLVED_POOL] == "coder"
    # PINNED: the first resolution is durable — later preference changes do
    # not re-route this conversation
    assert store.get_pool(prefix) == "coder"

    prefs.save(default_workspace=None, default_pool="default")
    env2 = _envelope(explicit=None)
    await ResolvePoolStage().process(env2, ctx)
    assert env2.metadata[RoutingMeta.RESOLVED_POOL] == "coder"  # still pinned
