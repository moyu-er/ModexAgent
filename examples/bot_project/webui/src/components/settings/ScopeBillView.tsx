// ScopeBillView — 账单渲染(票据16,SPEC §3.4 规则3 / §3.5 审计面)。
//
// 每个 agent 一张卡:逐字段(有效值 + 来源层 framework/profile/local)、
// 逐工具条目(实现来源 origin: bundled preset / supplement / 声明 /
// 派生)、O3 替换记录(`edit ← aci_edit`)。层级/来源/字段名等枚举与
// 标识符值按数据原样渲染(GraphNode 渲染 nodeType 的先例),文案标签走
// i18n。

import type {
  ScopeAgentBill,
  ScopeFieldValue,
} from "../../lib/scopeApi";
import { SectionLabel } from "../ui/SectionLabel";
import { useT } from "../../i18n";

/** 有效值的单行展示:标量原样,列表逗号连接,memory 面过滤 None 键。 */
export function formatFieldValue(value: ScopeFieldValue): string {
  if (Array.isArray(value)) return value.length > 0 ? value.join(", ") : "—";
  if (typeof value === "object") {
    const parts = Object.entries(value)
      .filter(([, v]) => v !== null)
      .map(([k, v]) => `${k}=${String(v)}`);
    return parts.length > 0 ? parts.join(", ") : "—";
  }
  return String(value);
}

function AgentBillCard({ bill }: { bill: ScopeAgentBill }) {
  const t = useT();
  return (
    <div
      data-testid={`scope-bill-agent-${bill.pool}-${bill.agent}`}
      className="rounded-lg border border-hairline bg-canvas-elevated p-4"
    >
      <div className="mb-3 flex items-center gap-2">
        <span className="font-mono text-sm font-semibold text-bright">
          {bill.agent}
        </span>
        {bill.root ? (
          <span className="rounded-sm border border-hairline px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-mute">
            {t("settings.scope.rootBadge")}
          </span>
        ) : null}
      </div>

      <div className="space-y-1">
        {bill.fields.map((f) => (
          <div
            key={f.field}
            data-testid={`scope-bill-field-${f.field}`}
            data-layer={f.layer}
            className="flex items-baseline gap-3 text-xs"
          >
            <span className="w-32 shrink-0 font-mono text-mute">{f.field}</span>
            <span className="min-w-0 flex-1 break-words font-mono text-body">
              {formatFieldValue(f.value)}
            </span>
            <span className="shrink-0 font-mono text-mute">
              {f.layer}
              {f.profile !== null ? ` (${f.profile})` : ""}
            </span>
          </div>
        ))}
      </div>

      <div className="mt-3 border-t border-hairline pt-2">
        <SectionLabel>{t("settings.scope.toolsHeader")}</SectionLabel>
        <div className="mt-1 space-y-1">
          {bill.tools.map((tool) => (
            <div
              key={tool.tool}
              data-testid={`scope-bill-tool-${tool.tool}`}
              data-origin={tool.origin}
              className="flex items-baseline gap-3 text-xs"
            >
              <span className="w-40 shrink-0 truncate font-mono text-body">
                {tool.tool}
              </span>
              <span className="shrink-0 font-mono text-mute">{tool.origin}</span>
              <span className="min-w-0 flex-1 truncate font-mono text-faint">
                {tool.replaces !== null
                  ? `← ${tool.replaces}`
                  : tool.targets.length > 0
                    ? `→ ${tool.targets.join(", ")}`
                    : ""}
              </span>
            </div>
          ))}
        </div>
      </div>

      {bill.replacements.length > 0 ? (
        <div className="mt-3 border-t border-hairline pt-2">
          <SectionLabel>{t("settings.scope.replacementsHeader")}</SectionLabel>
          <div className="mt-1 space-y-1">
            {bill.replacements.map((r) => (
              <div
                key={r.default_tool}
                data-testid={`scope-bill-replacement-${r.default_tool}`}
                className="font-mono text-xs text-body"
              >
                {`${r.default_tool} ← ${r.replacement_tool} (${r.supplement})`}
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function ScopeBillView({ agents }: { agents: ScopeAgentBill[] }) {
  // 编译输出即声明序(池声明序 × agent 声明序)——按池分组保持该序。
  const pools = new Map<string, ScopeAgentBill[]>();
  for (const agent of agents) {
    const group = pools.get(agent.pool);
    if (group) group.push(agent);
    else pools.set(agent.pool, [agent]);
  }
  return (
    <div data-testid="scope-bill" className="space-y-4">
      {[...pools.entries()].map(([pool, poolAgents]) => (
        <section key={pool}>
          <SectionLabel>{pool}</SectionLabel>
          <div className="mt-2 space-y-3">
            {poolAgents.map((agent) => (
              <AgentBillCard key={agent.agent} bill={agent} />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
