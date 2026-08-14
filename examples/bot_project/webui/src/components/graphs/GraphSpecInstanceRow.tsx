// GraphSpecInstanceRow — spec 详情右侧面板中的单条 instance 行(Rev 3 T11):
// #ID + 状态 badge + 相对时间(上行),MiniTopology 状态着色 + 进度·耗时(下行)。
// hover 态与 Sidebar 会话行一致(.session-row: --color-session-hover/active)。
// 点击行 → onOpenInstance。

import { useMemo, type FC } from "react";
import type { GraphInstance, GraphNodeStatus } from "../../lib/graphsApi";
import { useT, type TFn } from "../../i18n";
import { buildNodeStatusMap, GraphStatusBadge, statusLabelKey } from "./shared";
import { MiniTopology } from "./topology/MiniTopology";
import type { ParsedGraphTopology } from "./yaml/parseGraphSpec";

export interface GraphSpecInstanceRowProps {
  instance: GraphInstance;
  /** 节点状态(来自 getInstance 详情;未加载时 undefined,不渲染进度行)。 */
  nodes?: GraphNodeStatus[];
  /** spec 拓扑(行内 MiniTopology;解析失败时 null,不渲染缩略图)。 */
  topology?: ParsedGraphTopology | null;
  onOpenInstance: (instanceId: string) => void;
}

/** "just now" / "Nm ago" / "Nh ago" / "Nd ago"。 */
function formatRelativeTime(ms: number, t: TFn): string {
  // 与 lib/timezone normalizeMs 一致:秒级时间戳补成毫秒。
  const epochMs = ms < 1e12 ? ms * 1000 : ms;
  const diffSec = Math.max(0, Math.floor((Date.now() - epochMs) / 1000));
  if (diffSec < 60) return t("graphs.timeJustNow");
  const minutes = Math.floor(diffSec / 60);
  if (minutes < 60) return t("graphs.timeMinutesAgo", { count: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return t("graphs.timeHoursAgo", { count: hours });
  return t("graphs.timeDaysAgo", { count: Math.floor(hours / 24) });
}

export const GraphSpecInstanceRow: FC<GraphSpecInstanceRowProps> = ({
  instance,
  nodes,
  topology,
  onOpenInstance,
}) => {
  const t = useT();
  const total = nodes?.length ?? 0;
  const completed = nodes?.filter((n) => n.status === "completed").length ?? 0;
  const elapsedSec =
    instance.created_at !== undefined && instance.updated_at !== undefined
      ? Math.max(0, Math.round((instance.updated_at - instance.created_at) / 1000))
      : null;
  const nodeStatusMap = useMemo(
    () => (nodes !== undefined ? buildNodeStatusMap(nodes) : undefined),
    [nodes],
  );

  return (
    <button
      type="button"
      onClick={(): void => onOpenInstance(instance.graph_instance_id)}
      className="session-row flex w-full flex-col items-stretch gap-1 px-3 py-2.5 text-left hover:border-brand focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
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
            {formatRelativeTime(instance.created_at, t)}
          </span>
        )}
      </span>
      <span className="flex items-center gap-2">
        {topology ? (
          <MiniTopology
            topology={topology}
            nodeStatuses={nodeStatusMap}
            className="shrink-0"
          />
        ) : null}
        {nodes !== undefined && total > 0 ? (
          <span className="font-mono text-xs text-mute">
            {t("graphs.progress", { completed, total })}
            {elapsedSec !== null && ` · ${t("graphs.elapsed", { seconds: elapsedSec })}`}
          </span>
        ) : null}
      </span>
    </button>
  );
};
