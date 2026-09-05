"""SandboxGuardInterceptor — the opt-in policy layer (Ticket 08).

Coverage per ticket:
- A-class execution tools (``bash``) → GuardPipeline (pattern/path/traversal
  per GuardSettings toggles) → deny → ``ToolResult(error)`` actionable copy
- B-class file tools (read/write/edit/ls/glob/grep) → WorkspacePolicy
  boundary per policy roots; DANGER_FULL_ACCESS → no boundary check
- C-class web tools (web_reader) → NetworkGuard SSRF static check
- denial-feature translation (``translate_denial`` pure function)
- container-death → re-resolve → retry-once → still-failing copy
- enforcement snapshot written once (session-level cached resolve)
- DEFAULT dormancy: the factory refuses to build (the FIRST gate — the
  runtime's ResolvedSandbox validator is the second)
- shell-argv wiring: the guard carries the resolved argv for the shell seams
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.message import ToolCall
from modex_agent.core.tool_manager import ToolResult
from modex_agent.interceptor.abc import ToolCallContext
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.sandbox.decision import SecurityDecisionService
from modex_agent.sandbox.interceptor import (
    SandboxGuardInterceptor,
    translate_denial,
)
from modex_agent.sandbox.runtime import ResolvedSandbox, SandboxRuntime
from modex_agent.sandbox.settings import (
    GuardSettings,
    SandboxBackend,
    SandboxPolicy,
    SandboxSettings,
)
from modex_agent.sandbox.types import EnforcementLevel
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

WS = Path("/ws/project")


class _RuntimeStub:
    """Minimal runtime with a real turn-state custom dict (the write seam)."""

    def __init__(self) -> None:
        self.state = ReActTurnState()


class _CtxStub:
    """Minimal AgentContext stand-up: only what the interceptor reads."""

    runtime: _RuntimeStub | None

    def __init__(self, runtime: _RuntimeStub | None = None) -> None:
        self.session = "test-session"
        self.runtime = runtime
        self.turn_state: Any = runtime.state if runtime is not None else None


def _call(tool_name: str, arguments: dict[str, Any]) -> ToolCallContext:
    return ToolCallContext(
        tool_call=ToolCall(tool_name=tool_name, arguments=arguments, call_id="call-1"),
        tool_name=tool_name,
        arguments=arguments,
        session_id="test-session",
        turn_id="turn-1",
    )


def _resolved(
    backend: SandboxBackend = SandboxBackend.LOCAL,
    shell_argv: list[str] | None = None,
    one_shot: list[str] | None = None,
) -> ResolvedSandbox:
    return ResolvedSandbox(
        backend=backend,
        enforcement=EnforcementLevel.FULL if backend is not SandboxBackend.HOST
        else EnforcementLevel.NONE,
        shell_argv=shell_argv or [],
        one_shot_command_argv_prefix=one_shot or [],
    )


def _guard(
    settings: SandboxSettings,
    *,
    resolved: ResolvedSandbox | None = None,
    workspace_root: Path = WS,
    resolve: Callable[[], ResolvedSandbox] | None = None,
) -> SandboxGuardInterceptor:
    """Build a guard whose runtime resolve is deterministic in tests.

    Default: a pre-resolved sandbox (no probing). A ``resolve`` callable
    overrides (the container-death retry tests count invocations).
    """
    if resolve is not None:
        async def _resolve(settings_: SandboxSettings, ws: Path) -> ResolvedSandbox:
            return resolve()
        runtime = _FakeRuntime(_resolve)
    else:
        value = resolved or _resolved()
        async def _fixed(settings_: SandboxSettings, ws: Path) -> ResolvedSandbox:
            return value
        runtime = _FakeRuntime(_fixed)
    return SandboxGuardInterceptor(
        settings=settings,
        runtime=runtime,
        workspace_root_provider=_FixedRoot(workspace_root),
        decision=SecurityDecisionService(
            settings=settings,
            workspace_root_provider=_FixedRoot(workspace_root),
        ),
    )


class _FakeRuntime(SandboxRuntime):
    """Stands in for a SandboxRuntime — carries a controllable resolve."""

    def __init__(self, resolve_impl: Any) -> None:
        self._resolve_impl = resolve_impl

    async def resolve(self, settings: SandboxSettings, workspace_root: Path) -> ResolvedSandbox:
        return await self._resolve_impl(settings, workspace_root)


class _FixedRoot(WorkspaceRootProvider):
    """WorkspaceRootProvider returning a fixed root."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


def _ok(tool_name: str = "bash") -> ToolResult:
    return ToolResult.from_text(tool_name, "ok")


def _settings(
    policy: SandboxPolicy = SandboxPolicy.WORKSPACE_WRITE,
    backend: SandboxBackend = SandboxBackend.LOCAL,
    guard: GuardSettings | None = None,
) -> SandboxSettings:
    kwargs: dict[str, Any] = {"backend": backend, "policy": policy}
    if guard is not None:
        kwargs["guard"] = guard
    return SandboxSettings.model_validate(kwargs)


async def _run(
    guard_interceptor: SandboxGuardInterceptor,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    runtime: _RuntimeStub | None = None,
) -> ToolResult:
    """Drive one interception with a passing-through next call."""
    runtime = runtime or _RuntimeStub()
    ctx = _CtxStub(runtime)
    ctx.turn_state = runtime.state
    result = await guard_interceptor.around_tool_call(
        ctx,  # type: ignore[arg-type]
        _call(tool_name, arguments or {}),
        lambda: _ok_result_async(tool_name),
    )
    return result


async def _ok_result_async(tool_name: str) -> ToolResult:
    return _ok(tool_name)


# ---------------------------------------------------------------------------
# select_runtime — moved to the selection layer (typed selection → runtime).
# Coverage lives in test_selection.py; here we pin that the interceptor
# module no longer owns selection.
# ---------------------------------------------------------------------------


def test_select_runtime_moved_to_selection_layer() -> None:
    import modex_agent.sandbox.interceptor as interceptor_mod
    from modex_agent.sandbox.selection import select_runtime

    assert not hasattr(interceptor_mod, "select_runtime")
    assert callable(select_runtime)


# ---------------------------------------------------------------------------
# translate_denial — pure EPERM/read-only translation
# ---------------------------------------------------------------------------


class TestTranslateDenial:
    def test_read_only_file_system_gets_suggestion(self) -> None:
        out = translate_denial(
            "touch: cannot touch '/etc/hosts': Read-only file system",
            SandboxPolicy.READ_ONLY,
        )
        assert "[modex sandbox]" in out
        assert "read-only" in out

    def test_operation_not_permitted_gets_suggestion(self) -> None:
        out = translate_denial(
            "mkdir: cannot create directory '/x': Operation not permitted",
            SandboxPolicy.WORKSPACE_WRITE,
        )
        assert "[modex sandbox]" in out
        assert "sandbox boundary" in out

    def test_clean_stderr_passes_through_unchanged(self) -> None:
        text = "command not found: foo"
        assert translate_denial(text, SandboxPolicy.WORKSPACE_WRITE) == text

    def test_original_text_preserved_in_output(self) -> None:
        text = "cp: cannot create '/etc/x': Read-only file system"
        assert text in translate_denial(text, SandboxPolicy.READ_ONLY)


# ---------------------------------------------------------------------------
# A-class: bash command guard matrix
# ---------------------------------------------------------------------------


class TestBashGuardMatrix:
    async def test_dangerous_command_denied_with_actionable_copy(self) -> None:
        guard = _guard(_settings())
        result = await _run(guard, "bash", {"command": "rm -rf /"})
        assert result.error is not None
        assert "Sandbox policy denied" in result.error
        assert "rm" in result.error or "destructive" in result.error

    async def test_fork_bomb_denied(self) -> None:
        guard = _guard(_settings())
        result = await _run(guard, "bash", {"command": ":(){ :|:& };:"})
        assert result.error is not None
        assert "Sandbox policy denied" in result.error

    async def test_traversal_denied_when_enabled(self) -> None:
        guard = _guard(_settings())
        result = await _run(guard, "bash", {"command": "cat ../../etc/passwd"})
        assert result.error is not None

    async def test_path_escape_denied_under_workspace_write(self) -> None:
        # Path outside workspace under workspace-write policy (Linux-shaped
        # path so the extractor sees it).
        guard = _guard(_settings())
        result = await _run(guard, "bash", {"command": "cat /etc/passwd"})
        assert result.error is not None

    async def test_path_outside_allowed_under_read_only(self) -> None:
        # READ_ONLY has no writable root — reading inside workspace is the
        # boundary; /etc is outside.
        guard = _guard(_settings(policy=SandboxPolicy.READ_ONLY))
        result = await _run(guard, "bash", {"command": "cat /etc/passwd"})
        assert result.error is not None

    async def test_benign_command_passes_through(self) -> None:
        guard = _guard(_settings())
        result = await _run(guard, "bash", {"command": f"ls {WS}"})
        assert result.error is None
        assert result.message_content() == "ok"

    async def test_ssrf_url_denied(self) -> None:
        guard = _guard(_settings())
        result = await _run(
            guard, "bash", {"command": "curl http://169.254.169.254/latest/meta-data"}
        )
        assert result.error is not None

    async def test_guard_disabled_still_denies_builtin_rules(self) -> None:
        # Deterministic denials are structural: even with every guard
        # toggle off, the built-in deny rules still block rm -rf.
        guard = _guard(_settings(guard=GuardSettings(enabled=False)))
        result = await _run(guard, "bash", {"command": "rm -rf /"})
        assert result.error is not None

    async def test_all_toggles_off_advisory_layers_really_off(self) -> None:
        # All toggles off: builtin deny still fires, but the advisory
        # traversal layer no longer denies.
        guard = _guard(
            _settings(guard=GuardSettings(enabled=False, network=False))
        )
        denied = await _run(guard, "bash", {"command": "rm -rf /"})
        assert denied.error is not None
        traversed = await _run(guard, "bash", {"command": "cat ../../etc/passwd"})
        assert traversed.error is None

    async def test_unknown_execution_tool_passes_guard(self) -> None:
        # Non-matrix tools are not the guard's concern (E/F class).
        guard = _guard(_settings())
        result = await _run(guard, "todo_write", {"todos": []})
        assert result.error is None

    async def test_bash_result_translation_not_applied_to_success(self) -> None:
        # The interceptor must NOT touch a successful execution result.
        guard = _guard(_settings())
        result = await _run(guard, "bash", {"command": f"ls {WS}"})
        assert result.message_content() == "ok"


# ---------------------------------------------------------------------------
# B-class: file tool boundary
# ---------------------------------------------------------------------------


class TestFileToolBoundary:
    async def test_read_inside_workspace_passes(self) -> None:
        guard = _guard(_settings())
        result = await _run(guard, "read", {"path": "src/main.py"})
        assert result.error is None

    async def test_read_outside_workspace_denied(self) -> None:
        guard = _guard(_settings())
        result = await _run(guard, "read", {"path": "/etc/passwd"})
        assert result.error is not None
        assert "Sandbox policy denied" in result.error

    async def test_write_outside_workspace_denied(self) -> None:
        guard = _guard(_settings())
        result = await _run(guard, "write", {"path": "/etc/x", "content": "x"})
        assert result.error is not None

    async def test_read_only_policy_denies_write_inside_workspace(self) -> None:
        # READ_ONLY: write tools are refused outright (PRD: 全盘只读).
        guard = _guard(_settings(policy=SandboxPolicy.READ_ONLY))
        result = await _run(guard, "write", {"path": "src/new.py", "content": "x"})
        assert result.error is not None
        assert "read-only" in result.error

    async def test_read_only_policy_allows_read_inside_workspace(self) -> None:
        guard = _guard(_settings(policy=SandboxPolicy.READ_ONLY))
        result = await _run(guard, "read", {"path": "src/main.py"})
        assert result.error is None

    async def test_edit_read_only_denied(self) -> None:
        guard = _guard(_settings(policy=SandboxPolicy.READ_ONLY))
        result = await _run(
            guard, "edit", {"path": "a.py", "old_string": "a", "new_string": "b"}
        )
        assert result.error is not None

    async def test_danger_full_access_no_file_boundary(self) -> None:
        # DANGER_FULL_ACCESS → policy declares no boundary — /etc reads pass.
        guard = _guard(_settings(policy=SandboxPolicy.DANGER_FULL_ACCESS))
        result = await _run(guard, "read", {"path": "/etc/passwd"})
        assert result.error is None
        result2 = await _run(guard, "write", {"path": "/etc/x", "content": "x"})
        assert result2.error is None

    async def test_traversal_path_denied_for_file_tool(self) -> None:
        guard = _guard(_settings())
        result = await _run(guard, "read", {"path": "../../etc/passwd"})
        assert result.error is not None

    async def test_glob_and_grep_are_bounded(self) -> None:
        guard = _guard(_settings())
        result = await _run(guard, "glob", {"pattern": "*.py", "path": "/etc"})
        assert result.error is not None


# ---------------------------------------------------------------------------
# C-class: web tools
# ---------------------------------------------------------------------------


class TestWebGuard:
    async def test_web_reader_private_ip_denied(self) -> None:
        guard = _guard(_settings())
        result = await _run(guard, "web_reader", {"url": "http://169.254.169.254/"})
        assert result.error is not None
        assert "Sandbox policy denied" in result.error

    async def test_web_reader_public_url_passes(self) -> None:
        guard = _guard(_settings())
        result = await _run(guard, "web_reader", {"url": "https://example.com/doc"})
        assert result.error is None

    async def test_web_search_uses_command_string_guard(self) -> None:
        # web_search has no URL argument; it rides the command-pattern guard
        # via its query (no deny expected for a benign query).
        guard = _guard(_settings())
        result = await _run(guard, "web_search", {"query": "bubblewrap docs"})
        assert result.error is None

    async def test_web_reader_localhost_literal_denied(self) -> None:
        guard = _guard(_settings())
        result = await _run(guard, "web_reader", {"url": "http://127.0.0.1:8080/admin"})
        assert result.error is not None


# ---------------------------------------------------------------------------
# Enforcement snapshot — session-level resolve-once
# ---------------------------------------------------------------------------


class TestEnforcementSnapshot:
    async def test_snapshot_written_on_first_call(self) -> None:
        runtime = _RuntimeStub()
        guard = _guard(_settings(), resolved=_resolved(backend=SandboxBackend.LOCAL))
        await _run(guard, "bash", {"command": f"ls {WS}"}, runtime=runtime)
        assert TurnCustomKey.SANDBOX_ENFORCEMENT in runtime.state.custom

    async def test_snapshot_records_backend_and_enforcement(self) -> None:
        runtime = _RuntimeStub()
        guard = _guard(_settings())
        await _run(guard, "bash", {"command": f"ls {WS}"}, runtime=runtime)
        snap = runtime.state.custom[TurnCustomKey.SANDBOX_ENFORCEMENT]
        assert snap.model_dump()["backend"] == "local"
        assert snap.model_dump()["enforcement"] == "full"

    async def test_resolve_runs_once_across_calls(self) -> None:
        calls = {"n": 0}

        def _resolve() -> ResolvedSandbox:
            calls["n"] += 1
            return _resolved()

        guard = _guard(_settings(), resolve=_resolve)
        await _run(guard, "bash", {"command": f"ls {WS}"})
        await _run(guard, "read", {"path": "x.py"})
        assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Container-death → re-resolve → retry once → still-fail copy
# ---------------------------------------------------------------------------


class TestContainerDeathRetry:
    async def test_no_such_container_never_replays_or_changes_substrate(self) -> None:
        resolves = {"n": 0}
        execs = {"n": 0}

        def _resolve() -> ResolvedSandbox:
            resolves["n"] += 1
            return _resolved(backend=SandboxBackend.OCI)

        async def _next() -> ToolResult:
            execs["n"] += 1
            if execs["n"] == 1:
                # docker CLI: container vanished (daemon restart / OOM kill).
                return ToolResult(
                    tool_name="bash",
                    error="Error response from daemon: No such container: modex-sbx-ws",
                )
            return _ok()

        guard = _guard(_settings(backend=SandboxBackend.OCI), resolve=_resolve)
        runtime = _RuntimeStub()
        ctx = _CtxStub(runtime)
        result = await guard.around_tool_call(
            ctx,  # type: ignore[arg-type]
            _call("bash", {"command": "ls"}),
            _next,
        )
        assert resolves["n"] == 1
        assert execs["n"] == 1
        assert "uncertain" in (result.error or "")

    async def test_retry_failure_gets_explicit_copy(self) -> None:
        def _resolve() -> ResolvedSandbox:
            return _resolved(backend=SandboxBackend.OCI)

        async def _next() -> ToolResult:
            return ToolResult(
                tool_name="bash",
                error="Error response from daemon: No such container: modex-sbx-ws",
            )

        guard = _guard(_settings(backend=SandboxBackend.OCI), resolve=_resolve)
        ctx = _CtxStub(_RuntimeStub())
        result = await guard.around_tool_call(
            ctx,  # type: ignore[arg-type]
            _call("bash", {"command": "ls"}),
            _next,
        )
        assert result.error is not None
        assert "uncertain" in result.error
        assert "not replayed" in result.error

    async def test_non_container_error_not_retried(self) -> None:
        execs = {"n": 0}

        async def _next() -> ToolResult:
            execs["n"] += 1
            return ToolResult(tool_name="bash", error="Exit code: 1")

        guard = _guard(_settings(backend=SandboxBackend.OCI))
        ctx = _CtxStub(_RuntimeStub())
        result = await guard.around_tool_call(
            ctx,  # type: ignore[arg-type]
            _call("bash", {"command": "ls"}),
            _next,
        )
        assert execs["n"] == 1
        assert result.error == "Exit code: 1"

    async def test_container_death_not_retried_on_host_backend(self) -> None:
        execs = {"n": 0}

        async def _next() -> ToolResult:
            execs["n"] += 1
            return ToolResult(
                tool_name="bash",
                error="Error response from daemon: No such container: modex-sbx-ws",
            )

        # HOST backend: no container exists; the retry contract is oci-only.
        guard = _guard(
            _settings(backend=SandboxBackend.HOST),
            resolved=_resolved(backend=SandboxBackend.HOST),
        )
        ctx = _CtxStub(_RuntimeStub())
        await guard.around_tool_call(
            ctx,  # type: ignore[arg-type]
            _call("bash", {"command": "ls"}),
            _next,
        )
        assert execs["n"] == 1


# ---------------------------------------------------------------------------
# Shell-argv wiring — the guard carries the resolved argv for the seams
# ---------------------------------------------------------------------------


class TestShellArgvCarrying:
    async def test_shell_argv_property_after_resolve(self) -> None:
        argv = ["bwrap", "--ro-bind", "/", "/", "--", "/bin/bash", "-i"]
        guard = _guard(_settings(), resolved=_resolved(shell_argv=argv))
        await guard.ensure_resolved()
        assert guard.shell_argv == tuple(argv)

    async def test_one_shot_command_prefix_property(self) -> None:
        prefix = ["docker", "exec", "modex-sbx-ws"]
        guard = _guard(_settings(), resolved=_resolved(one_shot=prefix))
        await _run(guard, "bash", {"command": "ls"})  # trigger resolve
        assert guard.one_shot_command_argv_prefix == tuple(prefix)

    async def test_argv_unavailable_before_first_resolve(self) -> None:
        guard = _guard(_settings())
        assert guard.shell_argv == ()


# ---------------------------------------------------------------------------
# Factory — plugins/defaults/interceptors.py
# ---------------------------------------------------------------------------


class TestSandboxGuardFactory:
    def test_registered_in_interceptor_slot(self) -> None:
        from modex_agent.plugins.abc import ComponentFactory, ComponentSlot
        from modex_agent.plugins.defaults.interceptors import (
            register_default_interceptors,
        )
        from modex_agent.plugins.loader import PluginRegistrationContext
        from modex_agent.plugins.registry import ComponentRegistry

        registry = ComponentRegistry()
        with PluginRegistrationContext(registry) as registration:
            register_default_interceptors(registration)
        factory = registry.resolve(ComponentSlot.INTERCEPTOR, "sandbox_guard")
        assert isinstance(factory, ComponentFactory)

    async def test_default_backend_factory_refuses(self) -> None:
        """The FIRST DEFAULT gate: the factory never builds a guard for the
        dormant tier — opt-in means the roster must declare a real backend."""
        from modex_agent.plugins.defaults.interceptors import (
            SandboxGuardConfig,
            SandboxGuardInterceptorFactory,
        )

        factory = SandboxGuardInterceptorFactory()
        config = SandboxGuardConfig.model_validate(
            {"sandbox": {"backend": "default"}}
        )
        with pytest.raises(ValueError, match="DEFAULT"):
            await factory.create(config, ctx=None)  # type: ignore[arg-type]

    async def test_default_backend_without_sandbox_section_refuses(self) -> None:
        from modex_agent.plugins.defaults.interceptors import (
            SandboxGuardConfig,
            SandboxGuardInterceptorFactory,
        )

        factory = SandboxGuardInterceptorFactory()
        config = SandboxGuardConfig()
        with pytest.raises(ValueError, match="DEFAULT"):
            await factory.create(config, ctx=None)  # type: ignore[arg-type]

    async def test_host_backend_creates_guard(self) -> None:
        from unittest.mock import MagicMock

        from modex_agent.plugins.assembly.context import (
            AgentContext,
            PoolRuntimeDeps,
        )
        from modex_agent.plugins.defaults.interceptors import (
            SandboxGuardConfig,
            SandboxGuardInterceptorFactory,
        )

        # HOST needs no probing — deterministic without monkeypatching.
        factory = SandboxGuardInterceptorFactory()
        config = SandboxGuardConfig.model_validate(
            {"sandbox": {"backend": "host", "policy": "workspace-write"}}
        )
        ctx = AgentContext(
            registry=MagicMock(),
            workspace_ctx=MagicMock(),
            pool_runtime=PoolRuntimeDeps(root_provider=_FixedRoot(WS)),
            agent_name="probe-agent",
        )
        guard = await factory.create(config, ctx)
        assert isinstance(guard, SandboxGuardInterceptor)
        assert guard.name == "sandbox_guard"

    def test_config_rejects_unknown_fields(self) -> None:
        from modex_agent.plugins.defaults.interceptors import SandboxGuardConfig

        with pytest.raises(ValidationError):
            SandboxGuardConfig.model_validate({"unknown": True})

    def test_config_sandbox_is_frozen_sandbox_settings(self) -> None:
        from modex_agent.plugins.defaults.interceptors import SandboxGuardConfig

        config = SandboxGuardConfig.model_validate(
            {"sandbox": {"backend": "host"}}
        )
        assert config.sandbox.backend is SandboxBackend.HOST
        with pytest.raises(ValidationError):
            config.sandbox = SandboxSettings()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Dormancy — DEFAULT never reaches a guard
# ---------------------------------------------------------------------------


class TestDefaultDormancy:
    def test_resolved_sandbox_validator_still_rejects_default(self) -> None:
        # Second gate (runtime.py) — regression anchor from Ticket 03.
        with pytest.raises(ValidationError):
            ResolvedSandbox(
                backend=SandboxBackend.DEFAULT,
                enforcement=EnforcementLevel.NONE,
                shell_argv=[],
                one_shot_command_argv_prefix=[],
            )

    async def test_default_settings_have_no_guard_surface(self) -> None:
        # Constructing the guard directly with DEFAULT settings is a program
        # error too — the tier is resolved at first use, and resolve raises.
        guard = SandboxGuardInterceptor(
            settings=SandboxSettings(),
            runtime=_FakeRuntime(None),
            workspace_root_provider=_FixedRoot(WS),
            decision=SecurityDecisionService(
                settings=SandboxSettings(),
                workspace_root_provider=_FixedRoot(WS),
            ),
        )
        with pytest.raises(ValueError, match="DEFAULT"):
            await _run(guard, "bash", {"command": "ls"})


# ---------------------------------------------------------------------------
# Seam wiring — BashToolFactory fallback consumes the guard's argv
# ---------------------------------------------------------------------------


class TestBashSeamWiring:
    def _ctx(self, guard: SandboxGuardInterceptor | None):
        from unittest.mock import MagicMock

        from modex_agent.interceptor.chain import InterceptorChain
        from modex_agent.plugins.assembly.context import (
            AgentContext,
            PoolRuntimeDeps,
        )

        chain = InterceptorChain([guard]) if guard is not None else None
        return AgentContext(
            registry=MagicMock(),
            workspace_ctx=MagicMock(),
            pool_runtime=PoolRuntimeDeps(interceptor_chain=chain),
            agent_name="probe-agent",
        )

    def _bash_factory(self):
        from modex_agent.plugins.abc import ComponentSlot
        from modex_agent.plugins.defaults.tools import register_default_tools
        from modex_agent.plugins.loader import PluginRegistrationContext
        from modex_agent.plugins.registry import ComponentRegistry

        registry = ComponentRegistry()
        with PluginRegistrationContext(registry) as registration:
            register_default_tools(registration)
        return registry.resolve(ComponentSlot.TOOL, "bash")

    async def test_fallback_bash_gets_sandbox_shell_argv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from modex_agent.plugins.defaults.tools import ToolConfig
        from modex_agent.tools.terminal.persistent_bash import PersistentBashTool

        monkeypatch.setattr(
            "modex_agent.plugins.defaults.tools.persistent_bash_supported",
            lambda: True,
        )
        argv = ["bwrap", "--ro-bind", "/", "/", "--", "/bin/bash", "-i"]
        guard = _guard(_settings(), resolved=_resolved(shell_argv=argv))
        # Eager factory-time resolve mirrors the interceptor factory contract.
        await guard.ensure_resolved()

        tool = await self._bash_factory().create(ToolConfig(), self._ctx(guard))
        assert isinstance(tool, PersistentBashTool)
        assert tool.manager._shell_argv == argv  # noqa: SLF001

    async def test_fallback_bash_without_guard_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from modex_agent.plugins.defaults.tools import ToolConfig
        from modex_agent.tools.terminal.persistent_bash import PersistentBashTool

        monkeypatch.setattr(
            "modex_agent.plugins.defaults.tools.persistent_bash_supported",
            lambda: True,
        )
        tool = await self._bash_factory().create(ToolConfig(), self._ctx(None))
        assert isinstance(tool, PersistentBashTool)
        assert tool.manager._shell_argv is None  # noqa: SLF001 — host mode


# ---------------------------------------------------------------------------
# Approval regression — zero interaction (existing suite is the witness;
# this anchor asserts the guard composes in the chain without touching
# approval-classified calls).
# ---------------------------------------------------------------------------


class TestApprovalZeroInteraction:
    async def test_guard_wraps_after_approval_in_chain(self) -> None:
        """The guard is one TOOL_CALL interceptor among others: it must
        compose through InterceptorChain without changing neighbor results
        (approval happens BEFORE the executor chain in ToolNode — the guard
        only ever sees already-approved calls)."""
        from modex_agent.interceptor.chain import InterceptorChain

        guard = _guard(_settings())
        await guard.ensure_resolved()
        chain = InterceptorChain([guard])
        runtime = _RuntimeStub()
        ctx = _CtxStub(runtime)
        result = await chain.around_tool_call(
            ctx,  # type: ignore[arg-type]
            _call("bash", {"command": f"ls {WS}"}),
            lambda: _ok_result_async("bash"),
        )
        assert result.error is None
        assert result.message_content() == "ok"

    async def test_guard_deny_does_not_break_chain_contract(self) -> None:
        from modex_agent.interceptor.chain import InterceptorChain

        guard = _guard(_settings())
        await guard.ensure_resolved()
        chain = InterceptorChain([guard])
        runtime = _RuntimeStub()
        ctx = _CtxStub(runtime)
        result = await chain.around_tool_call(
            ctx,  # type: ignore[arg-type]
            _call("bash", {"command": "rm -rf /"}),
            lambda: _ok_result_async("bash"),
        )
        # A deny is a legal ToolResult — the chain contract holds.
        assert result.error is not None
        assert result.tool_name == "bash"


# ---------------------------------------------------------------------------
# turn_state snapshot write path uses the real seam
# ---------------------------------------------------------------------------


class TestSnapshotUsesTurnState:
    async def test_snapshot_written_via_turn_state_field(self) -> None:
        """When call.turn_state carries the runtime state, the snapshot
        lands there (per-turn typed key)."""
        runtime = _RuntimeStub()
        guard = _guard(_settings())
        ctx = _CtxStub(runtime)
        ctx.turn_state = runtime.state
        await guard.around_tool_call(
            ctx,  # type: ignore[arg-type]
            _call("bash", {"command": f"ls {WS}"}),
            lambda: _ok_result_async("bash"),
        )
        assert TurnCustomKey.SANDBOX_ENFORCEMENT in runtime.state.custom
