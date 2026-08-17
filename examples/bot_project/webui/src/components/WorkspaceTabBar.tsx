import { useEffect, useMemo, useRef, useState, type FC, type ReactElement } from "react";
import { Folder, Home, Plus, Settings, X } from "lucide-react";
import type { WorkspaceTab, WorkspaceTabStatus } from "../hooks/useWorkspaceTabs";
import { computeTabLabels } from "../hooks/useWorkspaceTabs";
import { OpenWorkspaceMenu } from "./OpenWorkspaceMenu";
import { ThemeToggle } from "./ThemeToggle";
import { useToast } from "./ToastContext";
import { LogoMarkIcon } from "./ui/icons";
import { useT } from "../i18n";

export interface WorkspaceTabBarProps {
  tabs: WorkspaceTab[];
  activeId: string;
  statuses: Record<string, WorkspaceTabStatus>;
  /** Home path — the pinned first tab. */
  home: string;
  recentWorkspaces: { path: string }[];
  /** Open a workspace in a NEW tab (always appends, never dedupes). */
  onOpenWorkspace: (path: string) => void;
  /** Register + open a path picked from recents (runs cd first). */
  onOpenRecent: (path: string) => void;
  onActivate: (id: string) => void;
  onClose: (id: string) => void;
  onReorder: (id: string, to: number) => void;
  onOpenSettings: () => void;
}

export const WorkspaceTabBar: FC<WorkspaceTabBarProps> = ({
  tabs,
  activeId,
  statuses,
  home,
  recentWorkspaces,
  onOpenWorkspace,
  onOpenRecent,
  onActivate,
  onClose,
  onReorder,
  onOpenSettings,
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

  const labels = useMemo(() => computeTabLabels(tabs, home), [tabs, home]);
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
          <LogoMarkIcon className="h-4 w-4" />
        </span>
        <span className={connected ? "dot-signal" : "dot-dim"} aria-hidden="true" />
        ModexBot
      </span>

      <div
        ref={scrollRef}
        className={`wstabs-scroll ${overflowing ? "overflowing" : ""}`}
      >
        {tabs.map((tab, index) => {
          const isHome = tab.id === "__home__";
          const label = isHome ? t("tabs.homeTab") : (labels[tab.id] ?? tab.path);
          const status = statuses[tab.id];
          return (
            <div
              key={tab.id}
              role="tab"
              data-tab-id={tab.id}
              aria-selected={tab.id === activeId}
              tabIndex={0}
              className={`wstab ${tab.id === activeId ? "active" : ""} `}
              title={tab.path}
              draggable={!isHome}
              onClick={() => onActivate(tab.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") onActivate(tab.id);
              }}
              onAuxClick={(e) => {
                if (e.button === 1 && !isHome) onClose(tab.id);
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
                {isHome ? <Home size={13} /> : <Folder size={13} />}
              </span>
              <span className="wstab-label">{label}</span>
              {status && status.pendingApprovals > 0 ? (
                <span className="wstab-dot warn" title={t("approval.awaitingApproval")} />
              ) : status && status.running > 0 ? (
                <span className="wstab-dot run" />
              ) : null}
              {!isHome && (
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
              )}
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
        onOpenRecent={onOpenRecent}
        onBrowsePicked={onOpenWorkspace}
        onGoHome={() => onActivate("__home__")}
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
