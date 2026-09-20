import { useMemo, useState, type FC, type CSSProperties } from "react";
import { Pin, Plus, Search, Workflow, X } from "lucide-react";
import type { PoolInfo } from "../lib/api";
import { WorkspacePathHeader } from "./WorkspacePathHeader";
import { SessionTree, type TreeNode } from "./SessionTree";
import { Button } from "./ui/Button";
import { SelectMenu } from "./ui/SelectMenu";
import { useT } from "../i18n";
import { sessionDisplayTitle } from "../lib/sessionTree";

export interface SidebarProps {
  sessionTree: TreeNode[];
  pools: PoolInfo[];
  selected: string | null;
  /** Full workspace path — read-only display at the top of the pod sidebar. */
  workspacePath: string;
  /** The single pool selection: history filter AND new-conversation
   *  assistant. Changing it re-targets the hero composer. */
  activePool: string;
  isLoadingSessions?: boolean;
  mobileOpen: boolean;
  onCloseMobile: () => void;
  onSelect: (sessionId: string) => void;
  onNew: (pool: string) => void;
  onDelete: (sessionId: string) => void;
  onPoolChange: (pool: string) => void;
  revealSessionId?: string | null;
  /** Open the shared rename dialog for a session (PA-02 tree entry). */
  onRenameSession?: (sessionId: string) => void;
  style?: CSSProperties;
  graphsActive?: boolean;
  onOpenGraphs?: () => void;
  /** True when this workspace path IS the saved default (pin pressed). */
  isDefaultWorkspace?: boolean;
  /** One-click "set as default workspace" next to the path header — the
   *  same preference write as the recent-menu pin (PA-08). */
  onSetDefaultWorkspace?: (path: string) => void;
}

/** Match a tree node (recursively) against a search query over title + id. */
function nodeMatchesQuery(node: TreeNode, q: string): boolean {
  const selfMatches =
    sessionDisplayTitle(node.session_id, node.metadata).toLowerCase().includes(q) ||
    node.session_id.toLowerCase().includes(q);
  return selfMatches || node.children.some((c) => nodeMatchesQuery(c, q));
}

function filterTree(tree: TreeNode[], q: string): TreeNode[] {
  return tree
    .filter((n) => nodeMatchesQuery(n, q))
    .map((n) => ({ ...n, children: filterTree(n.children, q) }));
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
  onRenameSession,
  style,
  graphsActive = false,
  onOpenGraphs,
  isDefaultWorkspace = false,
  onSetDefaultWorkspace,
}) => {
  const [iconPulseKey, setIconPulseKey] = useState(0);
  const [search, setSearch] = useState("");
  const t = useT();

  const handleNew = (): void => {
    // Remount the icon with the pulse class so every click replays the breath,
    // even when React sees no other state change (already on the hero view).
    setIconPulseKey((k) => k + 1);
    onNew(activePool);
  };

  const q = search.trim().toLowerCase();
  const visibleTree = useMemo(
    () => (q ? filterTree(sessionTree, q) : sessionTree),
    [sessionTree, q],
  );

  return (
    <div
      style={style}
      className={`fixed inset-y-0 left-0 z-40 flex h-full w-[260px] flex-col border-r border-hairline-strong bg-canvas-sidebar transition-transform duration-200 ease-out md:static md:w-[var(--sidebar-width)] md:translate-x-0 ${
        mobileOpen ? "translate-x-0" : "-translate-x-full"
      }`}
    >
      {/* Path header + one-click default-workspace pin (PA-08): the pin is
          the SAME preference write as the recent-menu pin — it never
          switches the open workspace or touches running sessions. */}
      <div className="flex w-full items-stretch border-b border-hairline">
        <WorkspacePathHeader path={workspacePath} />
        {onSetDefaultWorkspace && (
          <button
            type="button"
            aria-label={t("tabs.setDefault")}
            aria-pressed={isDefaultWorkspace}
            title={
              isDefaultWorkspace
                ? t("tabs.defaultWorkspaceMenu", { path: workspacePath })
                : t("tabs.setDefault")
            }
            onClick={() => onSetDefaultWorkspace(workspacePath)}
            className={`flex w-9 shrink-0 items-center justify-center border-l border-hairline transition-colors duration-fast ease-out ${
              isDefaultWorkspace
                ? "text-brand"
                : "text-faint hover:bg-hairline-soft hover:text-body"
            }`}
          >
            <Pin size={13} aria-hidden="true" className={isDefaultWorkspace ? "rotate-45" : undefined} />
          </button>
        )}
      </div>

      {/* The single pool selector: filters the history list AND targets new
          conversations from the hero composer. Always visible (even with one
          pool) so the current assistant is explicit. */}
      <div className="border-b border-hairline px-4 py-2">
        <SelectMenu
          ariaLabel={t("sidebar.agentPool")}
          value={activePool}
          onChange={onPoolChange}
          options={[
            ...(activePool === ""
              ? [{ value: "", label: t("settings.pools.selectPool") }]
              : []),
            ...pools.map((p) => ({ value: p.name, label: p.name })),
          ]}
          className="w-full"
        />
      </div>

      {/* Graphs nav (PA-09 §4). Settings lives ONLY in the top-right
          WorkspaceTabBar gear — no sidebar entry. */}
      <div className="border-b border-hairline px-4 py-2">
        {onOpenGraphs && (
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
        )}
      </div>

      {/* New conversation + search row (PA-09 §4) */}
      <div className="space-y-2 border-b border-hairline px-4 py-3">
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
        <div className="relative">
          <Search
            size={13}
            aria-hidden="true"
            className="absolute left-2.5 top-1/2 -translate-y-1/2 text-faint"
          />
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("sidebar.searchPlaceholder")}
            aria-label={t("sidebar.search")}
            className="w-full rounded-pill border border-hairline bg-canvas py-1.5 pl-8 pr-3 text-sm text-ink placeholder:text-faint focus:border-brand focus:outline-none"
          />
          {search && (
            <button
              type="button"
              aria-label={t("sidebar.searchClear")}
              onClick={() => setSearch("")}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-faint hover:text-body"
            >
              <X size={12} aria-hidden="true" />
            </button>
          )}
        </div>
      </div>

      {/* Session tree */}
      <div className="flex-1 overflow-y-auto py-2">
        {isLoadingSessions ? (
          <p className="px-4 py-3 text-base text-mute">
            {t("sidebar.loading")}
          </p>
        ) : visibleTree.length === 0 ? (
          <p className="px-4 py-3 text-base text-mute">
            {q
              ? t("settings.nav.searchNoMatch", { query: search.trim() })
              : t("sidebar.noConversations", { pool: activePool })}
          </p>
        ) : (
          <SessionTree
            tree={visibleTree}
            selected={selected}
            onSelect={onSelect}
            onDelete={onDelete}
            revealSessionId={revealSessionId}
            onRename={onRenameSession}
          />
        )}
      </div>
    </div>
  );
};
