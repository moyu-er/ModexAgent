"""Comprehensive tests for the mem0_memory plugin.

Tests cover:
1. Utility functions (message conversion, prefetch formatting)
2. Provider unit tests (mocked mem0 — no external deps)
3. Config building (embedder/LLM config inheritance)
4. Integration with MemorySystem (fan-out, prefetch injection, pre-compress wiring)
5. End-to-end: add → prefetch → verify system prompt injection
"""

import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.memory.core.scope import MemoryContext
from framework.memory.system import MemorySystem, MemorySystemContextManager
from framework.plugins.context import PluginContext

# ---- Import plugin modules directly (not via PluginManager) ----
# We add the plugin directory to sys.path so we can import the modules
# without the PluginManager's af_plugin_* namespace trick.
_PLUGIN_DIR = str(
    Path(__file__).resolve().parents[3]
    / "examples" / "bot_project" / "plugins" / "mem0_memory"
)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# We can't use relative imports when loading outside the af_plugin namespace,
# so we import each module directly.
import importlib

# Create a fake package so relative imports work
_pkg = types.ModuleType("mem0_memory_test_pkg")
_pkg.__path__ = [_PLUGIN_DIR]
sys.modules["mem0_memory_test_pkg"] = _pkg

import mem0_memory_test_pkg.config as _config_mod
import mem0_memory_test_pkg.utils as _utils_mod

Mem0Config = _config_mod.Mem0Config
convert_messages = _utils_mod.convert_messages
format_prefetch = _utils_mod.format_prefetch

# For provider, we need to mock the relative imports
# We'll import the provider module with patched imports
_config_mod_sys = types.ModuleType("mem0_memory_test_pkg.config")
_config_mod_sys.Mem0Config = Mem0Config
_utils_mod_sys = types.ModuleType("mem0_memory_test_pkg.utils")
_utils_mod_sys.convert_messages = convert_messages
_utils_mod_sys.format_prefetch = format_prefetch

# Patch sys.modules so provider.py's relative imports resolve
sys.modules["mem0_memory_test_pkg.config"] = _config_mod_sys
sys.modules["mem0_memory_test_pkg.utils"] = _utils_mod_sys

import mem0_memory_test_pkg.provider as _provider_mod

Mem0MemoryProvider = _provider_mod.Mem0MemoryProvider


# ====================================================================== #
#  1. Utility function tests                                              #
# ====================================================================== #


class TestConvertMessages:
    """Message format conversion: framework format → mem0 format."""

    def test_basic_messages(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = convert_messages(messages)
        assert result == [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]

    def test_tool_messages_filtered_out(self):
        messages = [
            {"role": "user", "content": "read the file"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1"}]},
            {"role": "tool", "content": "file contents here", "tool_call_id": "tc1"},
            {"role": "assistant", "content": "Here's the file content"},
        ]
        result = convert_messages(messages)
        assert len(result) == 2
        assert all(m["role"] != "tool" for m in result)

    def test_multimodal_messages_text_extracted(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            },
        ]
        result = convert_messages(messages)
        assert len(result) == 1
        assert result[0]["content"] == "describe this"

    def test_empty_content_filtered(self):
        messages = [
            {"role": "user", "content": ""},
            {"role": "user", "content": "   "},
            {"role": "assistant", "content": "valid"},
        ]
        result = convert_messages(messages)
        assert len(result) == 1
        assert result[0]["content"] == "valid"

    def test_empty_list(self):
        assert convert_messages([]) == []


class TestFormatPrefetch:
    """Memory formatting for system prompt injection."""

    def test_with_scores(self):
        memories = [
            {"memory": "User prefers dark mode", "score": 0.85},
            {"memory": "User likes Python", "score": 0.72},
        ]
        result = format_prefetch(memories)
        assert "[相关记忆]" in result
        assert "dark mode" in result
        assert "相关度: 85%" in result
        assert "Python" in result

    def test_without_scores(self):
        memories = [
            {"memory": "User prefers dark mode", "score": 0},
        ]
        result = format_prefetch(memories)
        assert "dark mode" in result
        assert "相关度" not in result

    def test_empty_memory_text_skipped(self):
        memories = [
            {"memory": "", "score": 0.9},
            {"memory": "Valid memory", "score": 0.8},
        ]
        result = format_prefetch(memories)
        assert "Valid memory" in result
        assert result.count("\n") == 1  # header + 1 valid item (empty skipped but i increments)


# ====================================================================== #
#  2. Provider unit tests (mocked mem0)                                   #
# ====================================================================== #


def _make_provider(**config_overrides) -> Mem0MemoryProvider:
    config = Mem0Config(**config_overrides)
    return Mem0MemoryProvider(config)


def _make_context(**overrides) -> MemoryContext:
    defaults = {"session_id": "s1", "user_id": "u1"}
    defaults.update(overrides)
    return MemoryContext(**defaults)


class TestProviderBasics:

    def test_name(self):
        p = _make_provider()
        assert p.name == "mem0"

    def test_system_prompt_block(self):
        p = _make_provider()
        block = p.system_prompt_block()
        assert "语义记忆" in block
        assert len(block) > 20

    @patch("importlib.util.find_spec")
    def test_is_available_both_installed(self, mock_find_spec):
        mock_find_spec.return_value = MagicMock()
        p = _make_provider()
        assert p.is_available() is True

    @patch("importlib.util.find_spec")
    def test_is_available_missing_mem0(self, mock_find_spec):
        def side_effect(name):
            return None if name == "mem0" else MagicMock()
        mock_find_spec.side_effect = side_effect
        p = _make_provider()
        assert p.is_available() is False

    @patch("importlib.util.find_spec")
    def test_is_available_missing_chromadb(self, mock_find_spec):
        def side_effect(name):
            return None if name == "chromadb" else MagicMock()
        mock_find_spec.side_effect = side_effect
        p = _make_provider()
        assert p.is_available() is False


class TestProviderAdd:

    @pytest.mark.asyncio
    async def test_add_not_initialized(self):
        p = _make_provider()
        ctx = _make_context()
        result = await p.add([{"role": "user", "content": "hello"}], ctx)
        assert result["status"] == "error"
        assert "not initialized" in result["error"]

    @pytest.mark.asyncio
    async def test_add_converts_and_delegates(self):
        p = _make_provider()
        mock_mem0 = MagicMock()
        mock_mem0.add.return_value = {"results": [{"memory": "fact", "id": "1"}]}
        p._mem0 = mock_mem0

        ctx = _make_context(user_id="user42", agent_id="agent1")
        messages = [
            {"role": "user", "content": "I like Python"},
            {"role": "tool", "content": "should be filtered"},
        ]
        result = await p.add(messages, ctx)

        assert result["status"] == "ok"
        # Verify mem0.add was called with filtered messages
        call_args = mock_mem0.add.call_args
        passed_msgs = call_args[0][0]
        assert len(passed_msgs) == 1
        assert passed_msgs[0]["content"] == "I like Python"
        assert call_args[1]["user_id"] == "user42"
        assert call_args[1]["agent_id"] == "agent1"

    @pytest.mark.asyncio
    async def test_add_empty_messages_returns_ok(self):
        p = _make_provider()
        p._mem0 = MagicMock()
        ctx = _make_context()
        result = await p.add([], ctx)
        assert result["status"] == "ok"
        assert result["memories"] == []
        p._mem0.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_error_returns_error_status(self):
        p = _make_provider()
        mock_mem0 = MagicMock()
        mock_mem0.add.side_effect = RuntimeError("embedding failed")
        p._mem0 = mock_mem0

        ctx = _make_context()
        result = await p.add([{"role": "user", "content": "hi"}], ctx)
        assert result["status"] == "error"


class TestProviderSearch:

    @pytest.mark.asyncio
    async def test_search_not_initialized(self):
        p = _make_provider()
        ctx = _make_context()
        result = await p.search("query", ctx)
        assert result == []

    @pytest.mark.asyncio
    async def test_search_returns_formatted_results(self):
        p = _make_provider()
        mock_mem0 = MagicMock()
        mock_mem0.search.return_value = {
            "results": [
                {"memory": "fact 1", "score": 0.9, "metadata": {"session_id": "s1"}},
                {"memory": "fact 2", "score": 0.7, "metadata": {}},
            ]
        }
        p._mem0 = mock_mem0

        ctx = _make_context()
        results = await p.search("test query", ctx, limit=3, filters={"key": "val"})

        assert len(results) == 2
        assert results[0]["memory"] == "fact 1"
        assert results[0]["score"] == 0.9
        # Verify filters passed through (user_id merged in by provider)
        call_kwargs = mock_mem0.search.call_args[1]
        assert call_kwargs["filters"] == {"key": "val", "user_id": "u1"}
        assert call_kwargs["limit"] == 3

    @pytest.mark.asyncio
    async def test_search_error_returns_empty(self):
        p = _make_provider()
        mock_mem0 = MagicMock()
        mock_mem0.search.side_effect = RuntimeError("search error")
        p._mem0 = mock_mem0

        ctx = _make_context()
        results = await p.search("query", ctx)
        assert results == []


class TestProviderPrefetch:

    @pytest.mark.asyncio
    async def test_prefetch_not_initialized(self):
        p = _make_provider()
        ctx = _make_context()
        result = await p.prefetch("query", ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_prefetch_empty_query(self):
        p = _make_provider()
        p._mem0 = MagicMock()
        ctx = _make_context()
        assert await p.prefetch("", ctx) is None
        assert await p.prefetch(None, ctx) is None

    @pytest.mark.asyncio
    async def test_prefetch_merges_core_and_search(self):
        p = _make_provider()
        mock_mem0 = MagicMock()

        # get_all returns core memories
        mock_mem0.get_all.return_value = {
            "results": [
                {"memory": "core fact A", "score": 0},
                {"memory": "core fact B", "score": 0},
            ]
        }
        # search returns query-relevant memories
        mock_mem0.search.return_value = {
            "results": [
                {"memory": "core fact A", "score": 0.95},  # duplicate of core
                {"memory": "relevant fact C", "score": 0.8},
                {"memory": "low score", "score": 0.1},  # below threshold
            ]
        }
        p._mem0 = mock_mem0

        ctx = _make_context()
        result = await p.prefetch("some query", ctx)

        assert result is not None
        assert "core fact A" in result
        assert "core fact B" in result
        assert "relevant fact C" in result
        assert "low score" not in result  # below prefetch_min_score=0.3

    @pytest.mark.asyncio
    async def test_prefetch_returns_none_when_no_results(self):
        p = _make_provider()
        mock_mem0 = MagicMock()
        mock_mem0.get_all.return_value = {"results": []}
        mock_mem0.search.return_value = {"results": []}
        p._mem0 = mock_mem0

        ctx = _make_context()
        result = await p.prefetch("query", ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_prefetch_error_returns_none(self):
        p = _make_provider()
        mock_mem0 = MagicMock()
        mock_mem0.get_all.side_effect = RuntimeError("db error")
        p._mem0 = mock_mem0

        ctx = _make_context()
        result = await p.prefetch("query", ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_prefetch_core_exception_falls_back_to_search(self):
        """If get_all fails but search succeeds, still return search results."""
        p = _make_provider()
        mock_mem0 = MagicMock()
        mock_mem0.get_all.side_effect = RuntimeError("core error")
        mock_mem0.search.return_value = {
            "results": [{"memory": "search result", "score": 0.9}]
        }
        p._mem0 = mock_mem0

        ctx = _make_context()
        result = await p.prefetch("query", ctx)
        # gather with return_exceptions=True means core_result is the Exception
        # isinstance(core_result, dict) is False, so core is skipped
        # but search result is still processed
        assert "search result" in result


class TestProviderOnPreCompress:

    @pytest.mark.asyncio
    async def test_not_initialized_is_noop(self):
        p = _make_provider()
        ctx = _make_context()
        # Should not raise
        await p.on_pre_compress([{"role": "user", "content": "hi"}], ctx)

    @pytest.mark.asyncio
    async def test_extracts_facts_with_metadata(self):
        p = _make_provider()
        mock_mem0 = MagicMock()
        mock_mem0.add.return_value = {"results": []}
        p._mem0 = mock_mem0

        ctx = _make_context(session_id="s99")
        messages = [
            {"role": "user", "content": "important fact"},
            {"role": "tool", "content": "filtered out"},
        ]
        await p.on_pre_compress(messages, ctx)

        call_kwargs = mock_mem0.add.call_args[1]
        assert call_kwargs["metadata"]["source"] == "pre_compress"
        assert call_kwargs["metadata"]["session_id"] == "s99"
        # Tool message should be filtered
        passed_msgs = mock_mem0.add.call_args[0][0]
        assert len(passed_msgs) == 1

    @pytest.mark.asyncio
    async def test_none_metadata_filtered_for_chromadb(self):
        """ChromaDB rejects None in metadatas — verify they are stripped."""
        p = _make_provider()
        mock_mem0 = MagicMock()
        mock_mem0.add.return_value = {"results": []}
        p._mem0 = mock_mem0

        ctx = _make_context(session_id=None)
        await p.on_pre_compress([{"role": "user", "content": "hi"}], ctx)

        metadata = mock_mem0.add.call_args[1]["metadata"]
        assert "source" in metadata
        assert "session_id" not in metadata  # None was stripped

    @pytest.mark.asyncio
    async def test_error_is_silently_handled(self):
        p = _make_provider()
        mock_mem0 = MagicMock()
        mock_mem0.add.side_effect = RuntimeError("fail")
        p._mem0 = mock_mem0

        ctx = _make_context()
        # Should not raise
        await p.on_pre_compress([{"role": "user", "content": "hi"}], ctx)


class TestProviderShutdown:

    @pytest.mark.asyncio
    async def test_shutdown_cleans_up(self):
        p = _make_provider()
        mock_mem0 = MagicMock()
        p._mem0 = mock_mem0

        await p.shutdown()
        assert p._mem0 is None
        mock_mem0.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_handles_close_error(self):
        p = _make_provider()
        mock_mem0 = MagicMock()
        mock_mem0.close.side_effect = RuntimeError("close error")
        p._mem0 = mock_mem0

        # Should not raise
        await p.shutdown()
        assert p._mem0 is None

    @pytest.mark.asyncio
    async def test_shutdown_noop_when_not_initialized(self):
        p = _make_provider()
        await p.shutdown()  # Should not raise


# ====================================================================== #
#  3. Config builder tests                                                #
# ====================================================================== #


class TestEmbedderConfig:
    """Tests for the embedding provider abstraction."""

    def test_local_provider_returns_huggingface_config(self):
        p = _make_provider()
        config = p._embedding.get_mem0_config()
        assert config["provider"] == "huggingface"
        assert config["config"]["model"] == "sentence-transformers/all-MiniLM-L6-v2"

    @pytest.mark.asyncio
    async def test_remote_provider_inherits_from_llm_provider(self):
        p = _make_provider(embedding_provider="openai", embedding_model="text-embedding-3-small")
        mock_llm = MagicMock()
        mock_llm.base_url = "https://api.example.com/v1"
        mock_llm.api_key = "sk-test"

        await p._embedding.initialize(llm_provider=mock_llm)
        config = p._embedding.get_mem0_config()
        assert config["provider"] == "openai"
        assert config["config"]["openai_base_url"] == "https://api.example.com/v1"
        assert config["config"]["api_key"] == "sk-test"

    @pytest.mark.asyncio
    async def test_remote_provider_explicit_overrides_inheritance(self):
        p = _make_provider(
            embedding_provider="openai",
            embedding_model="text-embedding-3-small",
            embedding_base_url="https://custom.api/v1",
            embedding_api_key="sk-custom",
        )
        mock_llm = MagicMock()
        mock_llm.base_url = "https://default.api/v1"
        mock_llm.api_key = "sk-default"

        await p._embedding.initialize(llm_provider=mock_llm)
        config = p._embedding.get_mem0_config()
        assert config["config"]["openai_base_url"] == "https://custom.api/v1"
        assert config["config"]["api_key"] == "sk-custom"

    @pytest.mark.asyncio
    async def test_remote_provider_no_llm_provider(self):
        p = _make_provider(embedding_provider="openai", embedding_model="embo-01")
        await p._embedding.initialize()
        config = p._embedding.get_mem0_config()
        assert "openai_base_url" not in config["config"]
        assert "api_key" not in config["config"]


class TestLLMConfig:

    def test_strips_litellm_prefix(self):
        p = _make_provider()
        mock_llm = MagicMock()
        mock_llm.model = "openai/MiniMax-M2.5"
        mock_llm.api_key = "key123"
        mock_llm.base_url = "https://api.minimaxi.com/v1"

        config = p._build_llm_config({"llm_provider": mock_llm})
        assert config["config"]["model"] == "MiniMax-M2.5"
        assert config["config"]["api_key"] == "key123"
        assert config["config"]["openai_base_url"] == "https://api.minimaxi.com/v1"

    def test_no_prefix_passthrough(self):
        p = _make_provider()
        mock_llm = MagicMock()
        mock_llm.model = "gpt-4o"
        mock_llm.api_key = "key"
        mock_llm.base_url = None

        config = p._build_llm_config({"llm_provider": mock_llm})
        assert config["config"]["model"] == "gpt-4o"
        assert "openai_base_url" not in config["config"]

    def test_no_llm_provider_returns_empty(self):
        p = _make_provider()
        config = p._build_llm_config({})
        assert config == {}


# ====================================================================== #
#  4. Integration with MemorySystem (fan-out, wiring)                     #
# ====================================================================== #


class TestMemorySystemIntegration:
    """Test that Mem0MemoryProvider integrates correctly with MemorySystem."""

    @pytest.mark.asyncio
    async def test_add_provider_wires_pre_compress(self):
        """add_provider should register on_pre_compress callback."""
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()

            p = _make_provider()
            ms.add_provider(p)

            callbacks = ms._managers.short_term._config.pre_compress_callbacks
            assert callbacks is not None
            assert len(callbacks) == 1

            await ms.close()

    @pytest.mark.asyncio
    async def test_system_prompt_block_injected(self):
        """system_prompt_block should appear in MemorySystem.build_system_prompt()."""
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()

            p = _make_provider()
            ms.add_provider(p)

            ctx = _make_context()
            prompt = await ms.build_system_prompt(ctx)

            assert "语义记忆" in prompt
            assert p.system_prompt_block() in prompt

            await ms.close()

    @pytest.mark.asyncio
    async def test_add_via_fan_out(self):
        """MemorySystem.add_message should fan-out to provider.add()."""
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()

            p = _make_provider()
            mock_mem0 = MagicMock()
            mock_mem0.add.return_value = {"results": []}
            p._mem0 = mock_mem0
            ms.add_provider(p)

            ctx = _make_context()
            await ms.add_message(ctx, {"role": "user", "content": "hello"})

            # mem0.add should have been called
            mock_mem0.add.assert_called_once()
            passed_msgs = mock_mem0.add.call_args[0][0]
            assert len(passed_msgs) == 1
            assert passed_msgs[0]["content"] == "hello"

            await ms.close()

    @pytest.mark.asyncio
    async def test_search_via_memory_system(self):
        """MemorySystem.search_memories should fan-out to provider.search()."""
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()

            p = _make_provider()
            mock_mem0 = MagicMock()
            mock_mem0.search.return_value = {
                "results": [{"memory": "found", "score": 0.9, "metadata": {}}]
            }
            p._mem0 = mock_mem0
            ms.add_provider(p)

            ctx = _make_context()
            results = await ms.search_memories("query", ctx, limit=3)

            assert len(results) == 1
            assert results[0]["memory"] == "found"
            mock_mem0.search.assert_called_once()

            await ms.close()


# ====================================================================== #
#  5. End-to-end: prefetch → system prompt injection                      #
# ====================================================================== #


class TestPrefetchInjection:
    """Verify the full chain: provider.prefetch → <memory-context> in prompt."""

    @pytest.mark.asyncio
    async def test_prefetch_injected_into_build_system_prompt(self):
        """MemorySystemContextManager.build_system_prompt() should include mem0 prefetch."""
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()

            p = _make_provider()
            mock_mem0 = MagicMock()
            mock_mem0.get_all.return_value = {"results": []}
            mock_mem0.search.return_value = {
                "results": [
                    {"memory": "User prefers dark mode", "score": 0.9, "metadata": {}},
                ]
            }
            p._mem0 = mock_mem0
            ms.add_provider(p)

            ctx = _make_context()
            await ms.add_message(ctx, {"role": "user", "content": "what theme?"})

            adapter = MemorySystemContextManager(ms)
            # 先 load 填充 _context_cache，使 build_system_prompt 能读到历史
            await adapter.load("s1")
            prompt = await adapter.build_system_prompt(tool_manager=None)

            assert "<memory-context>" in prompt
            assert "dark mode" in prompt
            assert "</memory-context>" in prompt

            await ms.close()

    @pytest.mark.asyncio
    async def test_prefetch_not_injected_when_no_results(self):
        """No <memory-context> when provider returns None."""
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()

            p = _make_provider()
            mock_mem0 = MagicMock()
            mock_mem0.get_all.return_value = {"results": []}
            mock_mem0.search.return_value = {"results": []}
            p._mem0 = mock_mem0
            ms.add_provider(p)

            ctx = _make_context()
            await ms.add_message(ctx, {"role": "user", "content": "hello"})

            adapter = MemorySystemContextManager(ms)
            await adapter.load("s1")
            prompt = await adapter.build_system_prompt(tool_manager=None)

            assert "<memory-context>" not in prompt

            await ms.close()

    @pytest.mark.asyncio
    async def test_full_lifecycle(self):
        """End-to-end: add messages → compress → prefetch → verify prompt."""
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()

            p = _make_provider()
            mock_mem0 = MagicMock()

            # add() will be called during fan-out
            mock_mem0.add.return_value = {"results": []}

            # get_all returns a core memory
            mock_mem0.get_all.return_value = {
                "results": [
                    {"memory": "User is a Python developer", "score": 0, "metadata": {}},
                ]
            }

            # search returns query-relevant memory
            def search_side_effect(query, **kwargs):
                return {
                    "results": [
                        {
                            "memory": f"Relevant to: {query}",
                            "score": 0.85,
                            "metadata": {},
                        }
                    ]
                }
            mock_mem0.search.side_effect = search_side_effect

            p._mem0 = mock_mem0
            ms.add_provider(p)

            ctx = _make_context()

            # Step 1: Add messages (triggers fan-out to provider.add)
            await ms.add_message(ctx, {"role": "user", "content": "I love Python"})
            await ms.add_message(ctx, {"role": "assistant", "content": "Noted!"})
            mock_mem0.add.assert_called()

            # Step 2: Build system prompt with prefetch via adapter
            adapter = MemorySystemContextManager(ms)
            await adapter.load("s1")
            prompt = await adapter.build_system_prompt(tool_manager=None)

            # Step 3: Verify prompt contains both core and search memories
            assert "<memory-context>" in prompt
            assert "Python developer" in prompt  # core memory
            assert "Relevant to:" in prompt  # search result

            # Step 4: Verify system_prompt_block is also present
            full_prompt = await ms.build_system_prompt(ctx)
            assert "语义记忆" in full_prompt

            await ms.close()


# ====================================================================== #
#  6. Plugin register() entry point test                                  #
# ====================================================================== #


class TestRegister:
    """Test the register() entry point reads config and registers provider."""

    def test_register_creates_provider_with_config(self):
        ctx = PluginContext(plugin_name="mem0_memory")
        ctx._config = {
            "workspace": "/tmp/test_mem0",
            "vector_store": "chroma",
            "prefetch_top_k": 10,
            "prefetch_min_score": 0.5,
        }
        ctx._memory_providers = []

        # Load __init__.py into the package namespace
        spec = importlib.util.spec_from_file_location(
            "mem0_memory_test_pkg", str(Path(_PLUGIN_DIR) / "__init__.py"),
            submodule_search_locations=[_PLUGIN_DIR],
        )
        init_mod = importlib.util.module_from_spec(spec)
        # Execute __init__.py — relative imports resolve via sys.modules
        spec.loader.exec_module(init_mod)

        assert hasattr(init_mod, "register")
        init_mod.register(ctx)

        assert len(ctx._memory_providers) == 1
        provider = ctx._memory_providers[0]
        assert isinstance(provider, Mem0MemoryProvider)
        assert provider._config.workspace == "/tmp/test_mem0"
        assert provider._config.prefetch_top_k == 10
        assert provider._config.prefetch_min_score == 0.5

    def test_register_disabled_skips_registration(self):
        """enabled: false should prevent provider registration."""
        ctx = PluginContext(plugin_name="mem0_memory")
        ctx._config = {
            "enabled": False,
            "workspace": "/tmp/test_mem0",
        }
        ctx._memory_providers = []

        spec = importlib.util.spec_from_file_location(
            "mem0_memory_test_pkg_disabled", str(Path(_PLUGIN_DIR) / "__init__.py"),
            submodule_search_locations=[_PLUGIN_DIR],
        )
        init_mod = importlib.util.module_from_spec(spec)
        # Register in sys.modules so relative imports resolve
        sys.modules["mem0_memory_test_pkg_disabled"] = init_mod
        spec.loader.exec_module(init_mod)

        init_mod.register(ctx)

        assert len(ctx._memory_providers) == 0, "Provider should not be registered when enabled=false"
