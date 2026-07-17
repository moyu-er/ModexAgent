// Skill selector for a single agent node.
//
// DISK IS THE SINGLE SOURCE OF TRUTH. Which skills an agent has is decided
// entirely by what's under skills/<pool>/<agent>/ on disk (symlinks into
// local_skills/). There is no skills field in the pool tree and nothing is
// deferred to the PoolEditor Save button: toggling a skill here calls
// assignSkill/unassignSkill IMMEDIATELY (eager disk write), refreshes the
// listing from disk, and surfaces the implied restart via toast + indicator.
//
// Local skills (present in the agent root but NOT in the global registry) are
// shown read-only — they cannot be toggled here; remove them by editing the
// agent root on disk.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { SkillEntry } from "../../types/pool";
import {
  assignSkill,
  listAgentSkills,
  listSkills,
  unassignSkill,
} from "../../lib/skillsApi";
import { ApiError } from "../../lib/api";
import { useToast } from "../ToastContext";
import { restartToast } from "./restartToast";
import { Card } from "../ui/Card";
import { Checkbox } from "../ui/Checkbox";
import { IconButton } from "../ui/IconButton";
import { ChevronDownIcon } from "../ui/icons";
import { useT } from "../../i18n";

interface Props {
  pool: string;
  agent: string;
}

export function AgentSkillSelector({ pool, agent }: Props) {
  const toast = useToast();
  const t = useT();
  const [open, setOpen] = useState<boolean>(false);
  const [globalSkills, setGlobalSkills] = useState<SkillEntry[] | null>(null);
  const [agentSkills, setAgentSkills] = useState<SkillEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string>("");
  const [busy, setBusy] = useState<string>("");
  const containerRef = useRef<HTMLDivElement>(null);

  const refresh = useCallback(async (): Promise<void> => {
    const a = await listAgentSkills(pool, agent);
    setAgentSkills(a);
  }, [pool, agent]);

  useEffect(() => {
    let cancelled = false;
    setLoadError("");
    Promise.all([listSkills(), listAgentSkills(pool, agent)])
      .then(([g, a]) => {
        if (cancelled) return;
        setGlobalSkills(g);
        setAgentSkills(a);
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoadError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [pool, agent]);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (!containerRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  // Assigned = agent skills that exist in the global registry (the checked
  // checkboxes). The pool tree is not involved.
  const globalNames = useMemo(
    () => new Set((globalSkills ?? []).map((s) => s.name)),
    [globalSkills],
  );
  const assignedNames = useMemo(
    () =>
      new Set(
        (agentSkills ?? [])
          .filter((s) => globalNames.has(s.name))
          .map((s) => s.name),
      ),
    [agentSkills, globalNames],
  );
  const localSkills = useMemo(
    () => (agentSkills ?? []).filter((s) => !globalNames.has(s.name)),
    [agentSkills, globalNames],
  );

  const toggle = async (name: string): Promise<void> => {
    if (busy) return;
    const assigned = assignedNames.has(name);
    setBusy(name);
    try {
      if (assigned) {
        await unassignSkill(pool, agent, name);
      } else {
        await assignSkill(pool, agent, name);
      }
      await refresh(); // re-read disk — the single source of truth
      // Skill assign/unassign unconditionally mark the pool dirty (the skill
      // manager loads roots at pool boot, not hot-reloaded), so the restart
      // toast fires unconditionally.
      restartToast(toast, t);
    } catch (e) {
      toast.show({
        message: t(assigned ? "settings.agentSkill.unassignFailed" : "settings.agentSkill.assignFailed", { detail: e instanceof ApiError ? `${e.status} ${e.detail}` : String(e) }),
        tone: "warning",
      });
    } finally {
      setBusy("");
    }
  };

  const header = t("settings.agentSkill.skillsSelected", { count: assignedNames.size });

  return (
    <div ref={containerRef} className="relative">
      <Card className="p-0">
        <div
          role="button"
          tabIndex={0}
          className="flex w-full cursor-pointer items-center gap-2 px-3 py-2 text-left hover:bg-hairline-soft"
          onClick={() => setOpen((v) => !v)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              setOpen((v) => !v);
            }
          }}
          aria-expanded={open}
        >
          <IconButton
            label={open ? t("settings.agentSkill.collapse") : t("settings.agentSkill.expand")}
            icon={<ChevronDownIcon open={open} />}
            variant="ghost"
            size="sm"
            onClick={(e) => { e.stopPropagation(); setOpen((v) => !v); }}
          />
          <span className="text-xs font-medium text-ink">{header}</span>
        </div>
      </Card>
      {open && (
        <div className="absolute left-0 right-0 top-full z-10 mt-1 max-h-64 overflow-y-auto rounded-md border border-hairline bg-canvas-elevated shadow-floating">
          <div className="px-3 py-2">
            <p className="mb-2 text-[11px] text-mute">
              {t("settings.agentSkill.changesImmediate")}
            </p>
            {loadError ? (
              <p className="text-xs text-error">{t("settings.agentSkill.failedToLoad", { error: loadError })}</p>
            ) : !globalSkills || !agentSkills ? (
              <p className="text-xs text-mute">{t("settings.agentSkill.loading")}</p>
            ) : globalSkills.length === 0 && localSkills.length === 0 ? (
              <p className="text-xs text-mute">{t("settings.agentSkill.noSkills")}</p>
            ) : (
              <ul className="space-y-1">
                {globalSkills.map((s) => {
                  const checked = assignedNames.has(s.name);
                  return (
                    <li key={`g-${s.name}`}>
                      <Checkbox
                        label={s.name}
                        checked={checked}
                        disabled={busy === s.name}
                        onChange={() => void toggle(s.name)}
                        aria-label={s.name}
                      />
                    </li>
                  );
                })}
                {localSkills.map((s) => (
                  <li key={`l-${s.name}`} className="flex items-center gap-2 text-sm text-ink">
                    <span
                      aria-hidden="true"
                      className="inline-block h-4 w-4 shrink-0 rounded-xs border border-hairline bg-canvas-elevated"
                      title={t("settings.agentSkill.localSkillTitle")}
                    />
                    <span className="truncate">{s.name}</span>
                    <span className="rounded-full border border-hairline px-1.5 py-0.5 text-[10px] text-mute">
                      {t("settings.agentSkill.local")}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
