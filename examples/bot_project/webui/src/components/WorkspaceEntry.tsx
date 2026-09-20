// WorkspaceEntry.tsx — the empty open-workspace entry (PA-08). Rendered
// when no workspace tab exists (fresh install with no default, or the user
// closed every tab). Never forces the repo/home directory.

import type { FC } from "react";
import { FolderOpen } from "lucide-react";
import { useT } from "../i18n";

export interface WorkspaceEntryProps {
  onOpen: () => void;
}

export const WorkspaceEntry: FC<WorkspaceEntryProps> = ({ onOpen }) => {
  const t = useT();
  return (
    <div
      data-testid="workspace-entry"
      className="flex flex-1 flex-col items-center justify-center gap-6 bg-canvas px-4"
    >
      <div className="flex flex-col items-center gap-2 text-center">
        <span className="brand-mark" aria-hidden="true">
          <FolderOpen size={32} className="text-brand" />
        </span>
        <h1 className="text-lg font-semibold text-bright">
          {t("workspaceEntry.title")}
        </h1>
        <p className="max-w-[420px] text-sm text-mute">{t("workspaceEntry.sub")}</p>
      </div>
      <button type="button" className="btn-primary" onClick={onOpen}>
        {t("workspaceEntry.open")}
      </button>
    </div>
  );
};
