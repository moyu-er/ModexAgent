import { type FC } from "react";
import { Folder } from "lucide-react";
import { useToast } from "./ToastContext";
import { useT } from "../i18n";

export interface WorkspacePathHeaderProps {
  /** Full workspace path (display-only; switching lives in the tab bar). */
  path: string;
}

/**
 * Read-only full-path line at the top of each pod's sidebar — the "where am
 * I" affordance after the old workspace switcher was absorbed by the tab
 * bar. Click copies the path.
 */
export const WorkspacePathHeader: FC<WorkspacePathHeaderProps> = ({ path }) => {
  const t = useT();
  const { show } = useToast();

  const copy = (): void => {
    if (!path) return;
    navigator.clipboard
      .writeText(path)
      .then(() => show({ message: t("tabs.pathCopied") }))
      .catch(() => {});
  };

  return (
    <button
      type="button"
      className="wspath-header"
      title={`${path}\n${t("tabs.copyPath")}`}
      onClick={copy}
    >
      <Folder size={13} aria-hidden="true" className="shrink-0" />
      <span className="wspath-text">{path}</span>
    </button>
  );
};
