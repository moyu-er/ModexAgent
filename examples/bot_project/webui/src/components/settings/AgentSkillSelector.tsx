// Skill selector for a single agent node.
//
// DISK IS THE SINGLE SOURCE OF TRUTH. Which skills an agent has is decided
// entirely by what's under skills/<pool>/<agent>/ on disk (symlinks into
// global_skills/). There is no skills field in the pool tree and nothing is
// deferred to the PoolEditor Save button: toggling a global skill here calls
// assignSkill/unassignSkill IMMEDIATELY (eager disk write), refreshes the
// listing from disk, and surfaces the implied restart via toast + indicator.
//
// Local skills (present in the agent root but NOT in the global registry) are
// shown read-only — they cannot be toggled here; remove them by editing the
// agent root on disk.

import { useCallback, useEffect, useState } from "react";
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
import { Chevron } from "./icons";

interface Props {
  pool: string;
  agent: string;
}

export function AgentSkillSelector({ pool, agent }: Props) {
  const toast = useToast();
  const [open, setOpen] = useState<boolean>(false);
  const [globalSkills, setGlobalSkills] = useState<SkillEntry[] | null>(null);
  const [agentSkills, setAgentSkills] = useState<SkillEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string>("");
  const [busy, setBusy] = useState<string>("");

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

  // Assigned = agent skills that exist in the global registry (the checked
  // global checkboxes). The pool tree is not involved.
  const globalNames = new Set((globalSkills ?? []).map((s) => s.name));
  const assignedNames = new Set(
    (agentSkills ?? []).filter((s) => globalNames.has(s.name)).map((s) => s.name),
  );
  const localSkills = (agentSkills ?? []).filter((s) => !globalNames.has(s.name));

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
      restartToast(toast);
    } catch (e) {
      toast.show({
        message: `Skill ${assigned ? "unassign" : "assign"} failed: ${
          e instanceof ApiError ? `${e.status} ${e.detail}` : String(e)
        }`,
        tone: "warning",
      });
    } finally {
      setBusy("");
    }
  };

  const header = `Skills (${assignedNames.size} selected)`;

  return (
    <div className="rounded-md border border-divider bg-sidebar-bg">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-sidebar-hover"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <Chevron open={open} />
        <span className="text-xs font-medium text-text-primary">{header}</span>
      </button>
      {open ? (
        <div className="border-t border-divider px-3 py-2">
          <p className="mb-2 text-[11px] text-text-secondary">
            Skill changes apply immediately.
          </p>
          {loadError ? (
            <p className="text-xs text-error">Failed to load: {loadError}</p>
          ) : !globalSkills || !agentSkills ? (
            <p className="text-xs text-text-secondary">Loading…</p>
          ) : globalSkills.length === 0 && localSkills.length === 0 ? (
            <p className="text-xs text-text-secondary">No skills available.</p>
          ) : (
            <ul className="space-y-1">
              {globalSkills.map((s) => {
                const checked = assignedNames.has(s.name);
                return (
                  <li key={`g-${s.name}`}>
                    <label className="flex cursor-pointer items-center gap-2 text-xs text-text-primary">
                      <input
                        type="checkbox"
                        checked={checked}
                        disabled={busy !== "" && busy !== s.name}
                        onChange={() => void toggle(s.name)}
                        aria-label={s.name}
                        className="h-3.5 w-3.5"
                      />
                      <span className="truncate font-medium">{s.name}</span>
                      <span className="rounded-full border border-card-border px-1.5 py-0.5 text-[10px] text-text-secondary">
                        global
                      </span>
                    </label>
                  </li>
                );
              })}
              {localSkills.map((s) => (
                <li key={`l-${s.name}`} className="flex items-center gap-2 text-xs">
                  <span
                    aria-hidden="true"
                    className="inline-block h-3.5 w-3.5 rounded border border-text-disabled"
                    title="Local skill — edit the agent root on disk to remove"
                  />
                  <span className="truncate text-text-secondary">{s.name}</span>
                  <span className="rounded-full border border-card-border px-1.5 py-0.5 text-[10px] text-text-secondary">
                    local
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}
