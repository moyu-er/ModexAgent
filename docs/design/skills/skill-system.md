# Skill 系统设计：解析、发现、注入与显式调用

状态：已实现 · 日期：2026-09-04
范围：`modex_agent.plugins.defaults.capabilities.skills`、capability prompt anchors、bot_project Skill 管理面

## 1. 目标

1. 使用稳定、严格的 `SKILL.md` frontmatter 契约发现技能。
2. system prompt 只携带可触发检索的技能摘要；正文按需读取。
3. `/name args` 显式调用始终注入纯正文，不携带 frontmatter。
4. 支持按 agent 声明额外技能根，同时保留 `skills/<pool>/<agent>/` 约定根。
5. 将变化频繁的技能目录放在 system prompt 尾部，缩小 prompt-prefix 失效面。

## 2. 非目标

- 不增加模型可调用的专用 Skill tool；模型通过文件读取工具按需展开正文。
- 不改变现有 `/name` 命令格式或 canonical command XML。
- 不改变外部 agent 的能力边界；scope compiler 的 V12 规则继续结构性排除 native capabilities。
- 不让 Skill frontmatter 承担文件访问控制；sandbox 和工具权限仍是安全边界。

## 3. 数据模型与不变量

`SkillSummary` 承载发现阶段需要的 name、description、metadata、location 和 resources；`Skill` 在此基础上增加 Markdown body。

硬不变量：

> `Skill.content` 永远只保存闭合 frontmatter fence 后的 Markdown body。YAML 只进入结构化字段，不进入 system prompt 正文或 slash 调用的 `<skill>` 内容。

`SkillMetadata` 只包含：

```python
class SkillMetadata(BaseModel):
    disable_model_invocation: bool = False
    extra: dict[str, Any]
```

`extra` 是 opaque extension payload：未知 frontmatter 字段会被保留，但框架不解释、不执行，也不注入 prompt。

## 4. Frontmatter 解析契约

共享 `parse_frontmatter()` 负责文档级边界：

1. 去除文件起始 BOM；
2. 将 CRLF/CR 归一为 LF；
3. 首行与闭合行必须是精确的 `---` fence；
4. 使用 YAML 1.2 布尔语义，只有 `true`/`false` 解析为布尔值；
5. YAML 结果必须是 mapping，其他类型收敛为空 mapping；
6. body 保留闭合 fence 后的内容，不额外 trim。

`FileSkillSource` 在 mapping 上执行 Skill 字段校验：

| 字段 | 规则 |
|---|---|
| `name` | 可选；缺省为目录名（DIRECTORY）或文件 stem（FLAT）。若提供，必须是字符串；最长 64；语法 `^[a-z0-9]+(?:-[a-z0-9]+)*$`。不要求与目录名相同。 |
| `description` | 文件技能必填、非空字符串、最长 1024。 |
| `disable-model-invocation` | 可选；只有 YAML 原生布尔 `true` 启用。 |
| `resources` | 可选；逐项验证为 `SkillResource`。 |
| 其他字段 | 移除文档字段后原样进入 `SkillMetadata.extra`。 |

非法 YAML、非 mapping、未闭合 frontmatter、非法 name 或 description 都使该文件不进入 catalog，并产生携带文件路径与原因的 warning。`InlineSkillSource` 是程序化可信入口，不执行文件格式校验。

Bot `SkillsStore` 复用同一技能名校验；pool/agent segment 继续使用各自的标识符语法。

## 5. 发现、合并与缓存

默认磁盘布局：

```text
skills/<pool>/<agent>/<skill>/SKILL.md
```

`SkillsSupply` 每池构建一次并拥有 `agent_name -> SkillCatalog`。每个 catalog 组合：

- `FileSkillSource`：发现、解析、正文加载；
- `DirectorySkillCache`：按 resolved target + content digest 检测增删、内容变更和链接目标变化；
- `SkillFilter`：可选的 catalog 级 allow/deny 策略；
- `DefaultSkillBuilder`：模型可见摘要；
- `SkillResolver`：显式命令解析。

多根目录按声明顺序合并，同名技能 last-wins。resolved root 在 catalog 构建边界去重。

## 6. 自定义根目录

每个 native agent 可声明额外技能根：

```yaml
capabilities:
  skills:
    roots:
    - team-skills
    - ~/.agents/skills
```

规则：

- 相对路径以 pool assembly 的 `project_dir` 为基；
- 支持 `~/` 和绝对路径；
- 实际顺序为 `[custom roots..., skills/<pool>/<agent>]`；
- 约定根最后，因此 agent 的显式分配覆盖共享根同名技能；
- 不存在的根仍留在 cache 监视列表中，后续创建可被发现；
- `capabilities: {skills: {}}` 与未声明 roots 保持默认布局；
- `capabilities: {skills: false}` 同时移除 prompt section 和 bound resolver。

## 7. System prompt 可见性

`DefaultSkillBuilder` 从完整 catalog 派生模型可见集合：

```python
visible_skills = tuple(
    skill for skill in skills
    if not skill.metadata.disable_model_invocation
)
```

行为：

1. `disable-model-invocation: true` 的技能不向 system prompt 暴露 name、description、directory 或占位符；
2. 混合列表只渲染可见技能；
3. 所有技能都隐藏时返回空字符串，整个 Skills section 消失；
4. 过滤只发生在 prompt builder，不能下沉到 source、cache、catalog 或通用 `SkillFilter`，否则会错误影响显式调用。

注入格式保持 metadata-only：

```xml
<available_skills>
  <skill name="weather" directory="/.../weather">
    <description>Get current weather and forecasts.</description>
  </skill>
</available_skills>
```

正文、resources 和 `SkillMetadata.extra` 都不进入该 XML。相对路径以每个 skill 的 directory 为基解析。

## 8. Capability prompt anchors

`PromptSectionSpec` 提供两个固定锚点：

```python
class SectionPlacement(StrEnum):
    HEAD = "head"
    TAIL = "tail"
```

- `HEAD`（默认）：fork context 后、core memory 前；
- `TAIL`：system prompt 的最终 block。

Native assembly 将 `binding.active_sections` 与 `CapabilityWiring.prompt_providers` 按位置一一配对；数量不一致立即启动失败。随后按 placement 分桶，并在每个桶中按 `order` 稳定排序。

完整 provider 顺序：

1. Runtime / Model info
2. Base system prompt
3. Fork context（subagent）
4. HEAD capability sections
5. Core memory
6. Archive / Pruned catalog
7. Provider blocks / Prefetch
8. Agent role contracts
9. Graph workflow guidance
10. TAIL capability sections

Skills 贡献 `skills.injection` 到 TAIL，因此技能摘要是最终 system prompt 的最后一段。其他 capability 默认仍在 HEAD，未声明 placement 时行为不变。

## 9. 显式调用

两条输入路径共享 `SkillResolver.resolve_command()`：

- 框架 `SkillCommandHandler`；
- bot `SkillParseStage`。

两者最终都调用 `build_skill_command_xml()`：

```xml
<command_context type="skill" name="weather" directory="/.../weather">
<skill>
# Weather

Markdown body only.
</skill>
</command_context>

<user_input>
Beijing
</user_input>
```

`disable-model-invocation` 不影响该路径。显式 `/name args` 代表用户意图，因此 hidden skill 仍可命中并展开。正文和用户参数分别做 XML text escaping，name 与 directory 做 attribute escaping。

## 10. 安全边界

`disable-model-invocation` 是提示词可见性控制，不是访问控制。它只阻止框架在 system prompt 中主动展示技能；模型若通过用户提供的路径或目录遍历发现文件，普通读取工具仍可能访问。真正的路径和执行限制由工具权限、approval 与 sandbox 负责。

## 11. 验收矩阵

1. Frontmatter：BOM、换行归一、精确 fence、YAML 1.2 布尔、malformed/non-mapping、body-only。
2. Source：name/description 校验、resources、directory/flat layout、cache reload。
3. 可见性：mixed hidden、all hidden、metadata-only XML。
4. 显式调用：hidden skill 仍可通过两个 onramp 展开，command XML 不含 frontmatter。
5. Anchors：HEAD/TAIL 几何、order、provider-count fail-fast、once-only setter。
6. Roots：per-agent 隔离、共享根、默认根覆盖、缺失根后续创建。
7. 边界：Skills facade import-light；commands 与 bot input stage 不依赖 capability 实现。
