// GraphSpecInstanceRow — spec 详情右侧面板中的单条 instance 行:
// #ID + 状态 badge + 时间(上行),completed/total 进度 + 耗时(下行)。
// 点击行 → onOpenInstance。

import type { FC } from "react";
import type { GraphInstance, GraphNodeStatus } from "../../lib/graphsApi";
import { useT } from "../../i18n";
import { formatClock } from "../../lib/timezone";
import { GraphStatusBadge, statusLabelKey } from "./shared";

export interface GraphSpecInstanceRowProps {
  instance: GraphInstance;
  /** 节点状态(来自 getInstance 详情;未加载时 undefined,进度显示 "—")。 */
  nodes?: GraphNodeStatus[];
  onOpenInstance: (instanceId: string) => void;
}

export const GraphSpecInstanceRow: FC<GraphSpecInstanceRowProps> = ({
  instance,
  nodes,
  onOpenInstance,
}) => {
  const t = useT();
  const total = nodes?.length ?? 0;
  const completed = nodes?.filter((n) => n.status === "completed").length ?? 0;
  const elapsedSec =
    instance.created_at !== undefined && instance.updated_at !== undefined
      ? Math.max(0, Math.round((instance.updated_at - instance.created_at) / 1000))
      : null;

  return (
    <button
      type="button"
      onClick={(): void => onOpenInstance(instance.graph_instance_id)}
      className="flex w-full flex-col gap-1 rounded-sm border-l-2 border-transparent px-3 py-2.5 text-left transition-colors hover:border-brand hover:bg-hairline-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
    >
      <span className="flex items-center gap-2">
        <span className="min-w-0 truncate font-mono text-base text-ink">
          #{instance.graph_instance_id}
        </span>
        <GraphStatusBadge
          status={instance.status}
          label={t(statusLabelKey(instance.status))}
        />
        {instance.created_at !== undefined && (
          <span className="ml-auto shrink-0 font-mono text-xs text-faint">
            {formatClock(instance.created_at)}
          </span>
        )}
      </span>
      <span className="font-mono text-xs text-mute">
        {total > 0 ? t("graphs.progress", { completed, total }) : "—"}
        {elapsedSec !== null && ` · ${t("graphs.elapsed", { seconds: elapsedSec })}`}
      </span>
    </button>
  );
};
