import { useEffect, useMemo, useRef, useState, type FC, type ReactElement } from "react";
import { Folder, Home, Plus, Settings, X } from "lucide-react";
import { ThemeToggle } from "./ThemeToggle";
import { Mascot } from "./Mascot";
import { OpenWorkspaceMenu } from "./OpenWorkspaceMenu";
import {
  computeTabLabels,
  sameWorkspacePath,
  type WorkspaceTab,
  type WorkspaceTabStatus,
} from "../hooks/useWorkspaceTabs";
import { useToast } from "./ToastContext";
import { useT } from "../i18n";

export interface WorkspaceTabBarProps {
  tabs: WorkspaceTab[];
  activeId: string;
  statuses: Record<string, WorkspaceTabStatus>;
  home: string;
  recentWorkspaces: { path: string }[];
  /** The user's saved default workspace path (null = no default). */
  defaultWorkspace: string | null;
  onOpenWorkspace: (path: string) => void;
  onOpenRecent: (path: string) => void;
  onActivate: (id: string) => void;
  onClose: (id: string) => void;
  onReorder: (id: string, to: number) => void;
  onOpenSettings: () => void;
  /** Save `path` as the default workspace preference (PA-08). */
  onSetDefaultWorkspace: (path: string) => void;
}

export const WorkspaceTabBar: FC<WorkspaceTabBarProps> = ({
  tabs,
  activeId,
  statuses,
  home,
  recentWorkspaces,
  defaultWorkspace,
  onOpenWorkspace,
  onOpenRecent,
  onActivate,
  onClose,
  onReorder,
  onOpenSettings,
  onSetDefaultWorkspace,
}) => {
  const t = useT();
  const { restart } = useToast();
  const [menuOpen, setMenuOpen] = useState(false);
  const dragIdRef = useRef<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  // When the tab strip overflows, the "+" leaves its inline position after
  // the last tab and pins to the top-right corner, and the strip grows a
  // thin visible scrollbar.
  const [overflowing, setOverflowing] = useState(false);

  const labels = useMemo(() => computeTabLabels(tabs), [tabs]);
  const activeStatus = statuses[activeId];
  const connected = activeStatus?.connected ?? false;

  // Overflow detection: strip content wider than its lane. ResizeObserver
  // covers lane resizes; the deps-free recheck covers tab add/remove.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const check = (): void => setOverflowing(el.scrollWidth > el.clientWidth + 1);
    const ro = new ResizeObserver(check);
    ro.observe(el);
    return (): void => ro.disconnect();
  }, []);
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    setOverflowing(el.scrollWidth > el.clientWidth + 1);
  });

  // Vertical wheel scrolls the strip horizontally (tab-bar convention).
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent): void => {
      if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
      e.preventDefault();
      el.scrollLeft += e.deltaY;
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return (): void => el.removeEventListener("wheel", onWheel);
  }, []);

  // A newly appended tab scrolls into view at the right edge.
  const prevCountRef = useRef(tabs.length);
  useEffect(() => {
    const el = scrollRef.current;
    if (el && tabs.length > prevCountRef.current) {
      el.scrollLeft = el.scrollWidth;
    }
    prevCountRef.current = tabs.length;
  }, [tabs.length]);

  // Any activation (click, close-fallback, keyboard) keeps the active tab
  // visible inside the strip.
  useEffect(() => {
    const el = scrollRef.current;
    el?.querySelector(`[data-tab-id="${CSS.escape(activeId)}"]`)
      ?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [activeId]);

  const plusButton: ReactElement = (
    <button
      type="button"
      className="wstabs-plus"
      aria-label={t("tabs.openWorkspace")}
      title={t("tabs.openWorkspace")}
      onClick={() => setMenuOpen((v) => !v)}
    >
      <Plus size={15} aria-hidden="true" />
    </button>
  );

  return (
    <div className="wstabs" role="tablist" aria-label={t("tabs.openWorkspace")}>
      <span
        className="wstabs-brand"
        title={connected ? t("chat.connected") : t("chat.disconnected")}
      >
        <span className="brand-mark" aria-hidden="true">
          <Mascot size={16} />
        </span>
        <span className={connected ? "dot-signal" : "dot-dim"} aria-hidden="true" />
        ModexBot
      </span>

      <div
        ref={scrollRef}
        className={`wstabs-scroll ${overflowing ? "overflowing" : ""}`}
      >
        {tabs.map((tab, index) => {
          const label = labels[tab.id] ?? tab.path;
          const status = statuses[tab.id];
          const isDefault =
            defaultWorkspace !== null && sameWorkspacePath(tab.path, defaultWorkspace);
          return (
            <div
              key={tab.id}
              role="tab"
              data-tab-id={tab.id}
              aria-selected={tab.id === activeId}
              tabIndex={0}
              className={`wstab ${tab.id === activeId ? "active" : ""} `}
              title={tab.path}
              draggable
              onClick={() => onActivate(tab.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") onActivate(tab.id);
              }}
              onAuxClick={(e) => {
                if (e.button === 1) onClose(tab.id);
              }}
              onDragStart={() => {
                dragIdRef.current = tab.id;
              }}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                const src = dragIdRef.current;
                dragIdRef.current = null;
                if (src && src !== tab.id) onReorder(src, index);
              }}
            >
              <span className="wstab-icon" aria-hidden="true">
                {tab.path === home ? <Home size={13} /> : <Folder size={13} />}
              </span>
              <span className="wstab-label">{label}</span>
              {isDefault && (
                <span
                  className="rounded-pill bg-hairline-soft px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-mute"
                  title={
                    defaultWorkspace
                      ? t("tabs.defaultWorkspaceMenu", { path: defaultWorkspace })
                      : undefined
                  }
                >
                  {t("tabs.defaultBadge")}
                </span>
              )}
              {status && status.pendingApprovals > 0 ? (
                <span className="wstab-dot warn" title={t("approval.awaitingApproval")} />
              ) : status && status.running > 0 ? (
                <span className="wstab-dot run" />
              ) : null}
              <button
                type="button"
                tabIndex={-1}
                className="wstab-close"
                aria-label={t("tabs.closeWorkspace", { name: label })}
                onClick={(e) => {
                  e.stopPropagation();
                  onClose(tab.id);
                }}
              >
                <X size={10} aria-hidden="true" />
              </button>
            </div>
          );
        })}
        {!overflowing && plusButton}
      </div>

      {overflowing && plusButton}

      <OpenWorkspaceMenu
        open={menuOpen}
        onClose={() => setMenuOpen(false)}
        recentWorkspaces={recentWorkspaces}
        defaultWorkspace={defaultWorkspace}
        onOpenRecent={onOpenRecent}
        onBrowsePicked={onOpenWorkspace}
        onSetDefault={onSetDefaultWorkspace}
        onGoHome={() => home && onOpenRecent(home)}
        anchorRight={overflowing}
      />

      <div className="wstabs-right">
        <ThemeToggle />
        <span className="relative">
          <button
            type="button"
            aria-label={t("sidebar.settings")}
            title={t("sidebar.settings")}
            onClick={onOpenSettings}
            className="rounded-md p-1.5 text-mute transition-colors hover:bg-hairline-soft hover:text-ink"
          >
            <Settings size={16} aria-hidden="true" />
          </button>
          {restart.restartNeeded && (
            <span
              role="img"
              aria-label={t("sidebar.restartRequired")}
              title={t("sidebar.restartRequiredTitle")}
              className="absolute right-0.5 top-0.5 h-2 w-2 rounded-full bg-error"
            />
          )}
        </span>
      </div>
    </div>
  );
};
