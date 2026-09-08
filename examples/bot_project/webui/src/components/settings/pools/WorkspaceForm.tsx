// WorkspaceForm.tsx — workspace node form: read-only name plus the optional
// persistence / paths overrides. "Inherit service config" omits the block
// entirely (the declaration carries deviations only).

import { useT } from "../../../i18n";
import { DropdownPanel } from "../../ui/DropdownPanel";
import { Input } from "../../ui/Input";
import { FormSection } from "./FormSection";
import { asString, nestedMap, type WorkspaceBody } from "./scopeModel";

const PERSISTENCE_BACKENDS = ["sqlite", "file"] as const;

interface Props {
  workspace: WorkspaceBody;
  /** Apply a mutation to the workspace body inside the draft model. */
  updateWorkspace: (mut: (body: WorkspaceBody) => void) => void;
}

export function WorkspaceForm({ workspace, updateWorkspace }: Props) {
  const t = useT();
  const backend = asString(nestedMap(workspace, "persistence")?.backend);
  const dataDir = asString(nestedMap(workspace, "paths")?.data_dir_name);

  return (
    <div className="space-y-4" data-testid="pools-workspace-form">
      <h3 className="font-mono text-base font-semibold text-bright">
        {t("settings.poolsPanel.workspace")}
      </h3>
      <FormSection title={t("settings.poolsPanel.sectionBasic")}>
        <Input
          label={t("settings.poolsPanel.workspaceName")}
          helper={t("settings.poolsPanel.workspaceNameLocked")}
          value={asString(workspace.name)}
          readOnly
          disabled
        />
        <DropdownPanel
          label={t("settings.poolsPanel.persistenceBackend")}
          helper={t("settings.poolsPanel.inheritHelper")}
          value={backend}
          options={[
            { value: "", label: t("settings.poolsPanel.inheritService") },
            ...PERSISTENCE_BACKENDS.map((v) => ({ value: v, label: v })),
          ]}
          onChange={(v) =>
            updateWorkspace((b) => {
              if (!v) delete b.persistence;
              else b.persistence = { ...(nestedMap(b, "persistence") ?? {}), backend: v };
            })
          }
        />
        <Input
          label={t("settings.poolsPanel.dataDir")}
          helper={t("settings.poolsPanel.dataDirHelper")}
          value={dataDir}
          onChange={(e) =>
            updateWorkspace((b) => {
              const value = e.target.value.trim();
              if (!value) delete b.paths;
              else b.paths = { ...(nestedMap(b, "paths") ?? {}), data_dir_name: value };
            })
          }
        />
      </FormSection>
    </div>
  );
}
