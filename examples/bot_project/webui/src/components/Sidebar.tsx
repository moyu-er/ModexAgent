import { useState, type FC, type CSSProperties } from "react";
import { Plus, Workflow } from "lucide-react";
import type { PoolInfo } from "../lib/api";
import { WorkspacePathHeader } from "./WorkspacePathHeader";
import { SessionTree, type TreeNode } from "./SessionTree";
import { Button } from "./ui/Button";
import { SelectMenu } from "./ui/SelectMenu";
import { useT } from "../i18n";

export interface SidebarProps {
  sessionTree: TreeNode[];
  pools: PoolInfo[];
  selected: string | null;
  /** Full workspace path — read-only display at the top of the pod sidebar. */
  workspacePath: string;
  activePool: string;
  isLoadingSessions?: boolean;
  mobileOpen: boolean;
  onCloseMobile: () => void;
  onSelect: (sessionId: string) => void;
  onNew: (pool: string) => void;
  onDelete: (sessionId: string) => void;
  onPoolChange: (pool: string) => void;
  revealSessionId?: string | null;
  style?: CSSProperties;
  graphsActive?: boolean;
  onOpenGraphs?: () => void;
}

export const Sidebar: FC<SidebarProps> = ({
  sessionTree,
  pools,
  selected,
  workspacePath,
  activePool,
  isLoadingSessions = false,
  mobileOpen,
  onCloseMobile,
  onSelect,
  onNew,
  onDelete,
  onPoolChange,
  revealSessionId,
  style,
  graphsActive = false,
  onOpenGraphs,
}) => {
  const [iconPulseKey, setIconPulseKey] = useState(0);
  const t = useT();

  const handleNew = (): void => {
    // Remount the icon with the pulse class so every click replays the breath,
    // even when React sees no other state change (already on the hero view).
    setIconPulseKey((k) => k + 1);
    onNew(activePool);
  };

  return (
    <div
      style={style}
      className={`fixed inset-y-0 left-0 z-40 flex h-full w-[260px] flex-col border-r border-hairline-strong bg-canvas-sidebar transition-transform duration-200 ease-out md:static md:w-[var(--sidebar-width)] md:translate-x-0 ${
        mobileOpen ? "translate-x-0" : "-translate-x-full"
      }`}
    >
      <WorkspacePathHeader path={workspacePath} />

      {/* Graphs nav — above conversations, workspace-scoped feature like conversations */}
      {onOpenGraphs && (
        <div className="border-b border-hairline px-4 py-2">
          <Button
            variant="ghost"
            size="md"
            onClick={(): void => {
              onOpenGraphs();
              onCloseMobile();
            }}
            className={`-ml-2 h-auto w-full justify-start gap-2 rounded-sm px-2 py-1.5 text-base ${
              graphsActive
                ? "bg-hairline-soft text-ink"
                : "text-body hover:bg-hairline-soft hover:text-ink"
            }`}
          >
            <Workflow size={16} className="shrink-0" />
            {t("sidebar.graphs")}
          </Button>
        </div>
      )}

      {/* Header */}
      <div className="flex items-center justify-between border-b border-hairline px-4 py-3">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-mute">
          {t("sidebar.conversations")}
        </h2>
      </div>

      {/* Pool selector badge */}
      {pools.length > 1 && (
        <div className="border-b border-hairline px-4 py-3">
          <SelectMenu
            ariaLabel={t("sidebar.agentPool")}
            value={activePool}
            onChange={onPoolChange}
            options={pools.map((p) => ({ value: p.name, label: p.name }))}
          />
        </div>
      )}

      {/* Session tree */}
      <div className="flex-1 overflow-y-auto py-2">
        {isLoadingSessions ? (
          <p className="px-4 py-3 text-base text-mute">
            {t("sidebar.loading")}
          </p>
        ) : sessionTree.length === 0 ? (
          <p className="px-4 py-3 text-base text-mute">
            {t("sidebar.noConversations", { pool: activePool })}
          </p>
        ) : (
          <SessionTree
            tree={sessionTree}
            selected={selected}
            onSelect={onSelect}
            onDelete={onDelete}
            revealSessionId={revealSessionId}
          />
        )}
      </div>

      {/* New Conversation button */}
      <div className="border-t border-hairline p-3">
        <Button
          variant="primary"
          size="lg"
          onClick={handleNew}
          className="h-auto w-full rounded-sm py-2.5 text-base"
        >
          <Plus
            key={iconPulseKey}
            size={16}
            className={iconPulseKey > 0 ? "newconv-icon-pulse" : undefined}
          />
          {t("sidebar.newConversation")}
        </Button>
      </div>
    </div>
  );
};
