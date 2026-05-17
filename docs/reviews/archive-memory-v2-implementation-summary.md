# Archive Memory V2 — Implementation Summary for Review

## Executive Summary

将 Memory 系统的 Archive（归档）层从**单一摘要** (`history.jsonl`，`SummaryStrategy`/`SummarizerStrategy`) 升级为**配对的 context 和 knowledge 双通道归档** (`context_archive.jsonl` + `knowledge_archive.jsonl`)，共享同一个 `archive_id`。

- **上下文注入**只读取 `context_archive` 通道
- **DreamEngine 知识整合**只消费 `knowledge_archive` 通道
- **归档提交**采用 all-or-nothing 策略（必须两个通道都写入成功，session 才截断）
- **旧的 `ArchiveStrategy` / `SummaryStrategy` 及相关文件**已完全移除

涉及 10 个 git commits（已 cherry-pick 到 `develop_gyt`）+ 根据 `docs/reviews/archive-memory-follow-up.md` 检视文档和代码审查进行的进一步修复（尚未提交）。

---

## 一、架构变更总览

```
旧 (V1):                              新 (V2):
messages.jsonl                        messages.jsonl
    ↓ 压缩触发                            ↓ 压缩触发
SummarizerStrategy (LLM)              DualLLMArchiveGenerationStrategy (LLM×2)
    ↓ 单摘要                              ↓ 双通道摘要
ArchiveEntry                          ArchiveWrite(CONTEXT) + ArchiveWrite(KNOWLEDGE)
    ↓                                      ↓
history.jsonl                         context_archive.jsonl (注入用)
                                          knowledge_archive.jsonl (Dream用)
                                          共享 archive_id, 成对提交/清理
```

**关键不变量：**
- `DefaultCommitPolicy._has_complete_archive_pair()` — 必须同时包含 CONTEXT 和 KNOWLEDGE 通道，否则 `committed=False`，session 不截断
- `prune_consumed_pairs()` — 只在 Dream 消费 knowledge 通道后清理 pair，保留最近 3 对已消费的 archive

---

## 二、Git 提交历史（10 个 commits）

| # | Commit | 说明 |
|---|--------|------|
| 1 | `fc389e3` | **feat(memory): add archive typed models** — 新增 `ArchiveChannel`、`ArchiveWrite`、`ArchiveBundleResult`、`ArchiveState`、`ArchiveGenerationInputs`、`ArchiveGenerationResult` |
| 2 | `6d63355` | **feat(memory): build archive transcripts** — 新增 `DefaultArchiveInputPolicy`、`DefaultToolChainFormatter`、`DefaultToolResultSummarizer` |
| 3 | `d11c5a5` | **feat(memory): add dual archive generation** — 新增 `ArchiveGenerationStrategy` Protocol 和 `DualLLMArchiveGenerationStrategy`，以及 `PROMPT_CONTEXT_ARCHIVE` / `PROMPT_KNOWLEDGE_ARCHIVE` |
| 4 | `d727557` | **feat(memory): add paired archive manager operations** — `ScopedArchiveMemoryManager.append_bundle()`、channel-aware `get_recent/search/get_unprocessed/commit_cursor`、`prune_consumed_pairs` |
| 5 | `a2d2ae7` | **feat(memory): store archive channels separately** — `DefaultScopedStorage` 新增 `context_archive.jsonl` / `knowledge_archive.jsonl` / `.archive_state.json` 物理文件 |
| 6 | `c731c92` | **feat(memory): write generated archive bundles** — `DefaultMemoryCompressionCoordinator` 使用 `ArchiveGenerationStrategy` 替代旧的 `SummaryStrategy` |
| 7 | `c321645` | **feat(memory): inject context archive channel** — `FullInjectionPolicy` 只注入 `ArchiveChannel.CONTEXT` |
| 8 | `5117cca` | **feat(memory): consume knowledge archive in dream** — `DreamEngine` 只消费 `ArchiveChannel.KNOWLEDGE`，提交 `archive_id` cursor |
| 9 | `8035f50` | **feat(memory): remove legacy archive strategy** — 删除 `framework/memory/archive/__init__.py`（239行）、`test_archive_strategy.py`；移除 coordinator 的旧 `summary=` 回退路径 |
| 10 | `bd6571e` | **test(memory): fix 26 failing tests after archive V2 all-or-nothing migration** — 适配已有测试到 V2 的 all-or-nothing commit 行为和 `archive_generation=` 参数 |

---

## 三、根据检视文档的修复（`archive-memory-follow-up.md` #1-#5，当前未提交）

### Fix #1 — 添加回归测试

已包含在 commit `bd6571e` 中。`test_compression_policies.py`、`test_bot_project_memory_pipeline.py`、`test_lifecycle.py`、`test_summarizer_integration.py` 中的测试均已适配 V2 行为。

### Fix #2 — All-or-nothing 提交（已实现）

`DefaultCommitPolicy._has_complete_archive_pair()` 检查 `result.writes` 中是否同时包含 CONTEXT 和 KNOWLEDGE，不完整则返回 `Committed=False` / `NOTHING_TO_ARCHIVE`。

`DualLLMArchiveGenerationStrategy.generate()` 在 LLM 返回 `(nothing)` 时返回空 `writes=()`。

### Fix #3 — 移除 coordinator 的旧 archive 回退（已实现）

`DefaultMemoryCompressionCoordinator` 不再在 `archive_generation is None` 时走 `summary` 的旧路径。提交策略要求完整的 archive pair。

### Fix #4 — 添加 `ArchiveChannelStorage` Protocol（当前未提交）

**文件变更：**
- `framework/memory/archive_models.py` — 新增 `@runtime_checkable ArchiveChannelStorage` Protocol，定义 5 个方法：
  `read_archive_state()`, `write_archive_state()`, `append_channel_log()`, `read_channel_logs()`, `save_channel_logs()`
- `framework/memory/stores/scoped_in_memory.py` — 实现上述 5 个方法（`DefaultScopedStorage` 已有实现，补齐内存版）
- `framework/memory/layers/archive.py` — `ScopedArchiveMemoryManager` 中 **5 处 `getattr` 动态访问全部替换为 `isinstance(storage, ArchiveChannelStorage)`** 类型检查
- `framework/memory/lifecycle.py` — Archive retention 同步改为通过协议读写 channel logs
- `framework/memory/stores/scoped_file.py` — `read_archive_state` 添加类型注解修复 mypy `no-any-return`

### Fix #5 — 清理 stale docs & 运行检查（当前未提交）

- `examples/bot_project/docs/memory-system.md` — 更新所有过时引用：
  - `history.jsonl` → `context_archive.jsonl` / `knowledge_archive.jsonl`
  - `SummarizerStrategy` / `SummaryStrategy` / `ArchiveStrategy` → `ArchiveGenerationStrategy` / `DualLLMArchiveGenerationStrategy`
- `framework/memory/compression/policies.py` — ruff 自动修复 import 排序
- `framework/memory/context_governance.py` — ruff 自动修复 import 排序
- `framework/memory/system.py` — ruff 自动修复 import 排序

---

## 四、代码审查发现与修复（当前未提交）

审查工具发现的 5 个问题（0 Critical, 0 High, 2 Medium, 3 Low）已全部修复：

| 严重度 | 文件 | 问题 | 修复 |
|--------|------|------|------|
| MEDIUM | `default_system.py:358` | `get_history_entries` 返回 dict 缺少 `archive_id` 字段 | 添加 `"archive_id": e.entry_id` |
| MEDIUM | `archive_input.py:57` | `DefaultToolResultSummarizer._context_summary` 未按规范实现 head(800)+tail(400) 截断 | 重写为 head+tail 截断格式，添加 `head_chars`/`tail_chars` 属性 |
| LOW | `injection/full_injection.py:142` | `## Historical Conversation Summaries` 未更新为 V2 措辞 | 改为 `## Historical Context Summaries` |
| LOW | `default_system.py:471` | 同上，第二处 | 同上 |
| LOW | `stores/scoped_file.py:39` | docstring 仍引用 `archive.jsonl` | 更新为 `context_archive.jsonl` / `knowledge_archive.jsonl` |
| LOW | `consolidation/dream_engine.py:277` | `_archive_entry_to_dict` 缺少 `source_session_id` 和 `archive_id` | 添加两个字段 |
| — | `tests/archive/test_archive_input.py:44` | 测试断言与新的 head+tail 行为不匹配 | `"short summary tail" not in` → `"short summary tail" in` |

---

## 五、bot_project 适配

### 配置适配 (`examples/bot_project/config/bot_config.yml`)

4 个 Agent 的 `max_tokens` 下调以适配新 provider 的上下文窗口：

| Agent | 旧值 | 新值 | 理由 |
|-------|------|------|------|
| main | 100000 | 60000 | 适应 provider 限制 |
| coder-craftsman | 50000 | 30000 | 同上 |
| mcp-explorer | 50000 | 20000 | 同上 |
| helper-sync | 50000 | 20000 | 同上 |

> 注意：`auto_llm_compression: true` 仍然启用，现在默认使用 `DualLLMArchiveGenerationStrategy`。

### 文档适配 (`examples/bot_project/docs/memory-system.md`)

- 架构图：`history.jsonl` → `context_archive.jsonl` + `knowledge_archive.jsonl`
- LLM 摘要 → LLM 双通道摘要
- 压缩流程：`SummarizerStrategy → LLM 摘要` → `DualLLMArchiveGenerationStrategy.generate() → LLM 双通道摘要`
- Commit 流程：`archive.append(ArchiveEntry) 写入 history.jsonl` → `archive.append_bundle() 写入 context_archive.jsonl + knowledge_archive.jsonl`
- 策略表：`SummaryStrategy` / `ArchiveStrategy` → `ArchiveGenerationStrategy` / `DualLLMArchiveGenerationStrategy`
- 配置注释：`SummarizerStrategy (LLM) 生成 Archive 摘要` → `DualLLMArchiveGenerationStrategy (LLM) 生成 Archive 摘要`

---

## 六、新增文件清单

```
framework/memory/archive_models.py              # ArchiveChannel, ArchiveWrite, ArchiveState, Protocol
framework/memory/archive_input.py               # DefaultArchiveInputPolicy, tool chain formatting
framework/memory/archive_generation.py          # ArchiveGenerationStrategy, DualLLMArchiveGenerationStrategy

tests/unit/memory/archive/test_archive_models.py
tests/unit/memory/archive/test_archive_input.py
tests/unit/memory/archive/test_archive_generation.py
tests/unit/memory/archive/test_archive_bundle_manager.py
tests/unit/memory/stores/test_archive_channel_files.py
tests/unit/memory/injection/test_context_archive_injection.py
tests/unit/memory/consolidation/test_dream_engine_archive_v2.py

docs/reviews/archive-memory-follow-up.md
docs/superpowers/plans/2026-05-17-archive-memory-v2.md
docs/superpowers/specs/2026-05-17-archive-memory-v2-design.md
```

**删除的文件：**
```
framework/memory/archive/__init__.py         # 旧 ArchiveStrategy (239 行)
tests/unit/memory/test_archive_strategy.py   # 旧 ArchiveStrategy 测试 (100 行)
```

---

## 七、修改的文件详细说明

| 文件 | 修改范围 | 说明 |
|------|---------|------|
| `framework/memory/archive_models.py` | +129 | 新增 `ArchiveChannelStorage` Protocol (uncommitted) |
| `framework/memory/archive_input.py` | +24/-11 | head+tail 截断 + `head_chars`/`tail_chars` (uncommitted) |
| `framework/memory/archive_generation.py` | +107 (committed) | 创建 |
| `framework/memory/layers/archive.py` | +26/-8 | `getattr` → `isinstance` Protocol 检查 (uncommitted) |
| `framework/memory/layers/config.py` | +2 | `retained_consumed_archive_pairs` 参数 |
| `framework/memory/core/layers.py` | +31 | `ArchiveMemoryManager` 扩展 channel-aware 方法 |
| `framework/memory/core/models.py` | +21 | 重导出 archive v2 类型 |
| `framework/memory/core/system.py` | +8 | `MemorySystem` ABC 接口更新 |
| `framework/memory/stores/scoped_file.py` | +94/-7 | channel 文件读写 + docstring 修复 |
| `framework/memory/stores/scoped_in_memory.py` | +56 | 实现 `ArchiveChannelStorage` 协议 (uncommitted) |
| `framework/memory/registry/file.py` | +14 | 文件注册表适配 channel 文件 |
| `framework/memory/registry/in_memory.py` | +22 | 内存注册表适配 channel-aware `has_file` |
| `framework/memory/compression/policies.py` | +52/-42 | coordinator 接入 archive generation |
| `framework/memory/lifecycle.py` | +200/-8 | Archive retention 协议适配 + 双通道 cleanup |
| `framework/memory/injection/full_injection.py` | +8/-3 | CONTEXT-only 注入 + header 修复 |
| `framework/memory/consolidation/dream_engine.py` | +32/-5 | KNOWLEDGE-only 消费 + `source_session_id` |
| `framework/memory/default_system.py` | +17/-3 | `get_history_entries` channel param + header 修复 |
| `framework/agents/summarizer/agent.py` | +54 | `PROMPT_CONTEXT_ARCHIVE` + `PROMPT_KNOWLEDGE_ARCHIVE` |
| `framework/ioc/factories/memory.py` | +13/-1 | 默认 wiring `DualLLMArchiveGenerationStrategy` |
| `framework/memory/__init__.py` | +14 | 导出新类型 |
| `framework/memory/context_governance.py` | +2/-2 | ruff import 排序修复 (uncommitted) |
| `framework/memory/system.py` | +2/-1 | ruff import 排序修复 (uncommitted) |

---

## 八、测试覆盖

**新增测试文件 (7 个)：**

| 测试文件 | 覆盖范围 |
|---------|---------|
| `test_archive_models.py` | `ArchiveChannel`, `ArchiveWrite`, `ArchiveState`, `ArchiveGenerationInputs`, `ArchiveBundleResult` |
| `test_archive_input.py` | 角色过滤、tool-chain 分组、参数格式化、head+tail 截断、orphan 丢弃 |
| `test_archive_generation.py` | 双通道生成、`(nothing)` 空输出、`max_tokens` 配置 |
| `test_archive_bundle_manager.py` | 成对写入、共享 `archive_id`、knowledge cursor、`prune_consumed_pairs` |
| `test_archive_channel_files.py` | 物理文件 `context_archive.jsonl` / `knowledge_archive.jsonl` |
| `test_context_archive_injection.py` | `FullInjectionPolicy` 只注入 CONTEXT |
| `test_dream_engine_archive_v2.py` | `DreamEngine` 只消费 KNOWLEDGE |

**适配的已有测试文件 (6 个)：**
- `test_compression_policies.py` — coordinator 接入 `archive_generation=`
- `test_lifecycle.py` — 压缩+archive 级联流程、archive retention
- `test_bot_project_memory_pipeline.py` — 端到端 pipeline（cleanup → compression → injection）
- `test_summarizer_integration.py` — SummarizerStrategy 集成适配
- `test_coordinator_priority.py` — session-only 模式参数适配
- `test_dream_engine_registry.py` — Dream registry 测试适配

---

## 九、验证结果

| 检查项 | 结果 |
|--------|------|
| `pytest tests/unit/memory/ -v` | **417 passed, 1 skipped, 0 failed** |
| `mypy framework/memory/` | **0 errors** (64 source files) |
| `ruff check framework/memory/` | 49 个预先存在的 `ANN401`/`ANN204`/`UP040`/`SIM110`，**未引入新问题** |

---

## 十、与设计规范的对照

设计文档: `docs/superpowers/specs/2026-05-17-archive-memory-v2-design.md`
实现计划: `docs/superpowers/plans/2026-05-17-archive-memory-v2.md`

| 设计要点 | 状态 | 实现位置 |
|---------|------|---------|
| 两个物理归档流 (context + knowledge) | ✅ | `stores/scoped_file.py:120-171` |
| 共享 `archive_id` | ✅ | `layers/archive.py:94-113` |
| `ArchiveGenerationStrategy` Protocol | ✅ | `archive_generation.py:32-40` |
| All-or-nothing 提交 | ✅ | `compression/policies.py:307-312` |
| 工具调用参数白名单过滤 | ✅ | `archive_input.py:37-48` |
| 工具结果 head(800)+tail(400) 截断 | ✅ | `archive_input.py:57-73` (审查后修复) |
| Dream 只消费 KNOWLEDGE | ✅ | `consolidation/dream_engine.py:85` |
| Injection 只读取 CONTEXT | ✅ | `injection/full_injection.py` |
| Pair 清理（保留 3 对已消费） | ✅ | `layers/archive.py:301-314` |
| 无旧 `archive.jsonl` 兼容 | ✅ | `archive/__init__.py` 已删除 |
| 旧 `ArchiveStrategy` 移除 | ✅ | 完全移除 |
| 旧 `SummaryStrategy` 移除 | ✅ | 默认路径不再使用 |
| `ArchiveChannelStorage` Protocol | ✅ | `archive_models.py` (Fix #4) |
| bot_project 文档更新 | ✅ | `examples/bot_project/docs/memory-system.md` |
| bot_project 配置适配 | ✅ | `examples/bot_project/config/bot_config.yml` |

---

## 十一、待办 / 已知限制

1. **未提交修改** — 仍有 15 个文件的修改在 working copy 中（Fix #4, Fix #5, Code Review 修复），建议审查后提交。
2. **ruff ANN401 预存问题** — 49 个 `Any` 类型警告为历史遗留，后续可专项清理。
3. **旧 `archive.jsonl` 无迁移路径** — 按设计规范，V2 为 breaking upgrade，不读取或迁移旧格式。
4. **`lifecycle.py` archive retention 部分**直接访问 storage 而非通过 archive manager — 当前修复为使用 `ArchiveChannelStorage` 协议 checked access，未来可考虑通过 manager 层抽象。

---

*生成日期: 2026-05-17 | 审查分支: `develop_gyt`*
