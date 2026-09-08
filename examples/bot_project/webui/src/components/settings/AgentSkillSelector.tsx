import { useCallback, useEffect, useMemo, useState } from "react";
import type { SkillEntry } from "../../types/pool";
import {
  assignSkill,
  listAgentSkills,
  unassignSkill,
} from "../../lib/skillsApi";
import { ApiError } from "../../lib/api";
import { useToast } from "../ToastContext";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { Checkbox } from "../ui/Checkbox";
import { useT } from "../../i18n";

interface Props {
  pool: string;
  agent: string;
  globalSkills: SkillEntry[];
}

export function AgentSkillSelector({ pool, agent, globalSkills }: Props) {
  const toast = useToast();
  const t = useT();
  const [agentSkills, setAgentSkills] = useState<SkillEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(false);
  const [busySkill, setBusySkill] = useState<string | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true);
    setLoadError("");
    try {
      setAgentSkills(await listAgentSkills(pool, agent));
    } catch (e) {
      setLoadError(String(e));
    } finally {
      setLoading(false);
    }
  }, [pool, agent]);

  useEffect(() => {
    setAgentSkills(null);
    void refresh();
  }, [refresh, globalSkills]);

  const globalNames = useMemo(
    () => new Set(globalSkills.map((skill) => skill.name)),
    [globalSkills],
  );
  const assignedNames = useMemo(
    () =>
      new Set(
        (agentSkills ?? [])
          .filter((skill) => globalNames.has(skill.name))
          .map((skill) => skill.name),
      ),
    [agentSkills, globalNames],
  );
  const localSkills = useMemo(
    () =>
      (agentSkills ?? []).filter(
        (skill) => !globalNames.has(skill.name),
      ),
    [agentSkills, globalNames],
  );

  const toggle = async (name: string): Promise<void> => {
    if (busySkill) return;
    const assigned = assignedNames.has(name);
    setBusySkill(name);
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
      ) : agentSkills === null ? (
        <p className="text-base text-mute">
          {t("settings.agentSkill.loading")}
        </p>
      ) : globalSkills.length === 0 && localSkills.length === 0 ? (
        <p className="text-base text-mute">
          {t("settings.agentSkill.noSkills")}
        </p>
      ) : (
        <ul className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {[...globalSkills]
            .sort((a, b) => a.name.localeCompare(b.name))
            .map((skill) => (
              <li
                key={`global-${skill.name}`}
                className="rounded-sm border border-hairline bg-hairline-soft px-3 py-2"
              >
                <Checkbox
                  label={skill.name}
                  checked={assignedNames.has(skill.name)}
                  disabled={busySkill !== null || loading}
                  onChange={() => void toggle(skill.name)}
                  aria-label={skill.name}
                />
              </li>
            ))}
          {localSkills.map((skill) => (
            <li
              key={`local-${skill.name}`}
              title={t("settings.agentSkill.localSkillTitle")}
              className="flex items-center gap-2 rounded-sm border border-hairline bg-hairline-soft px-3 py-2 text-base text-ink"
            >
              <span
                aria-hidden="true"
                className="inline-block h-4 w-4 shrink-0 rounded-xs border border-hairline bg-canvas-elevated"
              />
              <span className="min-w-0 flex-1 truncate">{skill.name}</span>
              <span className="rounded-full border border-hairline px-1.5 py-0.5 text-xs text-mute">
                {t("settings.agentSkill.local")}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
