from modex_agent.scope import AgentOverlay, PoolOverlay, ScopeOverlay, apply_scope_overlay
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec


def test_hook_overlay_appends_veto_without_mutating_the_source_roster():
    spec = ScopeSpec(kind=ScopeKind.POOL, pool=PoolSpec(
        name="main", agents=[AgentSpec(name="main", hooks=["+session_title", "+length_guard"])],
    ))
    adjusted = apply_scope_overlay(spec, ScopeOverlay(pools={
        "main": PoolOverlay(agents={"main": AgentOverlay(hooks=["-session_title"])}),
    }))
    assert adjusted.pool is not None and spec.pool is not None
    assert adjusted.pool.root_agent.hooks == ["+session_title", "+length_guard", "-session_title"]
    assert spec.pool.root_agent.hooks == ["+session_title", "+length_guard"]
