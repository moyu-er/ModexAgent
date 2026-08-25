# 07 调研：Python 生态"插件 + 自带 schema + 类型化登记"的现成先例

Labels: wayfinder:research
Status: closed (resolved 2026-08-18)

## Question

Python 生态里成熟的插件装载与注册模式：pluggy、stevedore / importlib.metadata entry_points、以及"贡献点"（contribution points）风格的系统；重点是**每个插件自带配置 schema** 和**类型化登记**的先例。

要回答：有没有能直接用的轮子，还是按本 repo 的纪律（ABC 优先、零 Protocol、frozen Pydantic）手写一个薄登记处更合适（当前倾向手写，但要有证据）。若有轮子，给出与本 repo 类型纪律的兼容性评估。

## Comments

### Resolution (2026-08-18, research subagent)

调研完成，产出 **本地 gitignored 调研笔记**。证据钉到具体 commit（pluggy@e382e72 / stevedore@d3a55f3 / npe2@8512ae5 / mcp-sdk@b2025ab）。

**结论：薄手写登记处；只用 stdlib `importlib.metadata` 做 PyPI 发现。不引入 pluggy / stevedore / npe2 / MCP SDK 依赖。**

关键论据：

- **没有任何 Python 库同时提供"类型化贡献 + 每插件配置 schema + ABC 优先"**——生态要么无类型（pluggy/stevedore），要么领域绑定（npe2→napari，MCP→线协议）。
- **MCP SDK v2 的 `Extension` 是与我们形状最接近的活先例**（非 Protocol 基类 + identifier 类定义期校验 + frozen 绑定对象 + 每贡献 `params_type: type[BaseModel]` + 全部走同一公共注册面、重名/遮蔽硬 ValueError）——24k 星项目独立收敛到本 repo 纪律已规定的形状，是"手写不是 NIH"的最强证据。
- **npe2 验证 manifest/贡献点设计在生态规模可行**（typed pydantic ContributionPoints + 每插件 ConfigurationContribution + 逐插件错误捕获的 discover）——抄思想不抄代码（其 entry group 与贡献类型全是 napari 域）。
- **我们的现状被确认正确**：三源优先加载（bundled>user>entry_points）、每插件 try/except 隔离（与 stevedore 参考设计一致，含"永不捕 KeyboardInterrupt"细节）、`_enabled` 白名单、本地目录按包加载（无候选库提供此能力）。

**留给 02 的 7 条落地指针**（详见报告 §4.3）：Plugin(ABC) 类型化入口 + `config_model: ClassVar`；PluginConfig frozen+forbid；内置组件降格走同一注册面；同源重名应炸响（跨源保持 first-seen-wins）；保留白名单；加一个便宜的 `api_version` 契约常量；隔离保持现状。
