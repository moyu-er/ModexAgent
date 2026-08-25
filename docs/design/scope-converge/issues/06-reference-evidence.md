# 06 取证：参考项目三件套的具体形状（抄什么、怎么抄）

Labels: wayfinder:research
Status: closed (resolved 2026-08-18)

## Question

从参考项目源码（本地克隆）提取要移植的机制的具体形状，产出一份"移植对照表"：

- (a) ctx 注册 API 全集——每个可注册能力的方法签名与参数；
- (b) 插件自带配置 schema（schemastery）与主配置文件配置行的映射方式；
- (c) 会话事件类型的登记 / 合并 / 校验机制，以及 deriveMessages 与 UI keyed renderer 如何按类型消费；
- (d) 分层作用域注册表（agent→preset→global 就近遮蔽）的实现机制；
- (e) 会话事件之外还有什么"变量"机制（workflow engine 的状态、`agent.inject()` 等）。

每条给：文件引用 + 实际 API/类型形状 + 建议的 Python/Pydantic 等价物（适配 frozen Pydantic + ABC 纪律的框架）。

## Comments

### Resolution (2026-08-18, research subagent)

取证完成，产出 **本地 gitignored 取证笔记**。要点：

- **(a) 注册 API 全集**：三种递归出现的形态——named-provider table（subagents/LLM 路由/storage 后端/skills）、definition registry（commands/skills）、capability-flagged provider ABC（可选方法即能力检查）。LLM 的 `replace()` 句柄是为热编辑存在，重启生效的 Python 版可收敛为重注册。
- **(b) 配置**：每个插件自带 schemastery Config，与主配置文件行配置 + patch 层映射（两个插件形态均验证：function plugin / Service plugin）。
- **(c) 事件类型**：SessionEventMap 声明合并 + append 校验 + `session-projection`（keyed fold over events，refcount，stateVersion 不匹配拒绝共享）。
- **(d) 分层作用域**：`agent → preset → global` 就近遮蔽的注册表实现。
- **(e) 事件外变量**：storage hub（mount/form + named backend registry）、`agent.inject()` 下一请求注入、workflow/jobs 状态。

**对其它票的输入**：
- 01 已消费：参考项目插件持久态走存储域而非事件流 → KVStore + 命名空间层同构成立。
- 02 将消费：三种注册形态的 Python 等价物建议、schemastery↔Pydantic config 映射方式。
