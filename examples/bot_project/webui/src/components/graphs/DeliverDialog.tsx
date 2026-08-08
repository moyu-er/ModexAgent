// DeliverDialog — modal for the deliver-to-node operation (PRD §6.1 Deliver).
//
// Reuses the ConfirmDialog modal pattern (modal-scrim + modal-panel, Esc to
// close, backdrop click to cancel). Contains a node selector (DropdownPanel
// form variant) listing instance nodes, a content Textarea, and
// confirm/cancel buttons. On confirm, calls onConfirm(nodeName, content).

import { useEffect, useState, type FC } from "react";
import { useT } from "../../i18n";
import { Button } from "../ui/Button";
import { DropdownPanel } from "../ui/DropdownPanel";
import { Textarea } from "../ui/Textarea";

export interface DeliverDialogProps {
  /** Node names available for delivery (from instance.nodes). */
  nodeNames: string[];
  onConfirm: (nodeName: string, content: string) => void;
  onCancel: () => void;
}

export const DeliverDialog: FC<DeliverDialogProps> = ({
  nodeNames,
  onConfirm,
  onCancel,
}) => {
  const t = useT();
  const [nodeName, setNodeName] = useState(
    nodeNames.length > 0 ? nodeNames[0]! : "",
  );
  const [content, setContent] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // Esc to cancel.
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCancel();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  const options = nodeNames.map((n) => ({ value: n, label: n }));

  const handleConfirm = (): void => {
    if (!nodeName || !content.trim() || submitting) return;
    setSubmitting(true);
    onConfirm(nodeName, content);
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={t("graphs.deliverDialogTitle")}
      className="modal-scrim-enter fixed inset-0 z-50 flex items-center justify-center bg-overlay p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onCancel();
      }}
      data-testid="deliver-dialog"
    >
      <div className="modal-panel-enter w-full max-w-md rounded-lg border border-hairline bg-canvas-popover p-4 shadow-popover">
        <h3 className="text-base font-semibold text-ink">
          {t("graphs.deliverDialogTitle")}
        </h3>
        <div className="mt-4 flex flex-col gap-4">
          {options.length > 0 ? (
            <DropdownPanel
              options={options}
              value={nodeName}
              onChange={setNodeName}
              label={t("graphs.deliverNodeLabel")}
              listboxLabel={t("graphs.deliverNodeLabel")}
            />
          ) : (
            <p className="text-xs text-faint">{t("graphs.selectNode")}</p>
          )}
          <Textarea
            label={t("graphs.deliverContentLabel")}
            placeholder={t("graphs.deliverContentPlaceholder")}
            value={content}
            onChange={(e): void => setContent(e.target.value)}
            rows={4}
          />
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="secondary" size="sm" onClick={onCancel} autoFocus>
            {t("common.cancel")}
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={handleConfirm}
            disabled={!nodeName || !content.trim() || submitting}
            loading={submitting}
          >
            {t("graphs.deliverConfirm")}
          </Button>
        </div>
      </div>
    </div>
  );
};
