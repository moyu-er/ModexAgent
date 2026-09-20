import { useCallback, useEffect, useMemo, useState } from "react";
import type { SkillEntry } from "../../types/pool";
import {
  assignSkill,
  listAgentSkills,
  listSkills,
  unassignSkill,
} from "../../lib/skillsApi";
import { ApiError } from "../../lib/api";
import { useToast } from "../ToastContext";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { SelectionList } from "../ui/SelectionList";
import { useT } from "../../i18n";

interface Props {
  pool: string;
  agent: string;
}

export function AgentSkillSelector({ pool, agent }: Props) {
  const toast = useToast();
  const t = useT();
  const [library, setLibrary] = useState<SkillEntry[] | null>(null);
  const [agentSkills, setAgentSkills] = useState<SkillEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(false);
  const [busySkill, setBusySkill] = useState<string | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true);
    setLoadError("");
    try {
      const [lib, assigned] = await Promise.all([
        listSkills(),
        listAgentSkills(pool, agent),
      ]);
      setLibrary(lib);
      setAgentSkills(assigned);
    } catch (e) {
      // Surface the failure — a failed load must not read as "no skills".
      setLoadError(String(e));
    } finally {
      setLoading(false);
    }
  }, [pool, agent]);

  useEffect(() => {
    setLibrary(null);
    setAgentSkills(null);
    void refresh();
  }, [refresh]);

  const assignedNames = useMemo(
    () => new Set((agentSkills ?? []).map((skill) => skill.name)),
    [agentSkills],
  );
  const libraryNames = useMemo(
    () => new Set((library ?? []).map((skill) => skill.name)),
    [library],
  );
  const items = useMemo(() => {
    const global = [...(library ?? [])].sort((a, b) =>
      a.name.localeCompare(b.name),
    );
    // Local installed dirs have no restorable global-library source —
    // they render checked + disabled with a "local" note; removing them
    // means deleting the agent dir on disk.
    const local = (agentSkills ?? []).filter(
      (skill) => !libraryNames.has(skill.name),
    );
    return [
      ...global.map((skill) => ({
        id: `global-${skill.name}`,
        label: skill.name,
        description: skill.description,
      })),
      ...local.map((skill) => ({
        id: `local-${skill.name}`,
        label: skill.name,
        description: skill.description,
        disabled: true,
        note: t("settings.agentSkill.local"),
        title: t("settings.agentSkill.localSkillTitle"),
      })),
    ];
  }, [library, agentSkills, libraryNames, t]);
  const checkedIds = useMemo(
    () =>
      new Set(
        [...assignedNames].map((name) =>
          libraryNames.has(name) ? `global-${name}` : `local-${name}`,
        ),
      ),
    [assignedNames, libraryNames],
  );
  const busyIds = useMemo(
    () => (busySkill ? new Set([busySkill]) : undefined),
    [busySkill],
  );

  const toggle = async (id: string): Promise<void> => {
    if (busySkill || !id.startsWith("global-")) return;
    const name = id.slice("global-".length);
    const assigned = assignedNames.has(name);
    setBusySkill(id);
    try {
      if (assigned) {
        await unassignSkill(pool, agent, name);
      } else {
        await assignSkill(pool, agent, name);
      }
      await refresh();
      toast.show({
        message: t(
          assigned
            ? "settings.agentSkill.unassigned"
            : "settings.agentSkill.assigned",
          { name },
        ),
        tone: "success",
      });
    } catch (e) {
      toast.show({
        message: t(
          assigned
            ? "settings.agentSkill.unassignFailed"
            : "settings.agentSkill.assignFailed",
          {
            detail:
              e instanceof ApiError
                ? `${e.status} ${e.detail}`
                : String(e),
          },
        ),
        tone: "warning",
      });
    } finally {
      setBusySkill(null);
    }
  };

  return (
    <Card className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-base font-medium text-ink" aria-live="polite">
            {t("settings.agentSkill.skillsSelected", {
              count: assignedNames.size,
            })}
          </p>
          <p className="mt-1 text-xs text-mute">
            {t("settings.agentSkill.changesImmediate")}
          </p>
        </div>
        <Button
          variant="secondary"
          size="sm"
          loading={loading}
          disabled={busySkill !== null}
          onClick={() => void refresh()}
        >
          {t("settings.agentSkill.refreshAssignments")}
        </Button>
      </div>

      {loadError ? (
        <p role="alert" className="text-base text-error">
          {t("settings.agentSkill.failedToLoad", { error: loadError })}
        </p>
      ) : agentSkills === null || library === null ? (
        <p className="text-base text-mute">
          {t("settings.agentSkill.loading")}
        </p>
      ) : items.length === 0 ? (
        <p className="text-base text-mute">
          {t("settings.agentSkill.noSkills")}
        </p>
      ) : (
        <SelectionList
          items={items}
          checked={checkedIds}
          onToggle={(id) => void toggle(id)}
          ariaLabel={t("settings.agentSkill.skillsSelected", { count: assignedNames.size })}
          searchLabel={t("settings.poolsPanel.filterPlaceholder")}
          busyIds={busyIds}
        />
      )}
    </Card>
  );
}
