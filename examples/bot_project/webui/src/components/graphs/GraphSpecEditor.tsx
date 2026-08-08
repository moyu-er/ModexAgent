// GraphSpecEditor — raw YAML editor for one graph spec. Save PUTs the YAML
// (backend validates + compiles before persisting; 400 detail surfaces in
// the error panel). Run creates an instance and hands off to the execution
// viewer.

import { useEffect, useState, type FC } from "react";
import { ArrowLeft, Play } from "lucide-react";
import {
  getSpec,
  runGraph,
  updateSpec,
} from "../../lib/graphsApi";
import { useT } from "../../i18n";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { Textarea } from "../ui/Textarea";
import { formatGraphApiError } from "./shared";

export interface GraphSpecEditorProps {
  workspaceId: string;
  specId: string;
  onBack: () => void;
  onRun: (instanceId: string) => void;
}

export const GraphSpecEditor: FC<GraphSpecEditorProps> = ({
  workspaceId,
  specId,
  onBack,
  onRun,
}) => {
  const t = useT();
  const [name, setName] = useState<string>("");
  const [yamlContent, setYamlContent] = useState<string>("");
  const [userInput, setUserInput] = useState<string>("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isRunning, setIsRunning] = useState(false);
  const [savedTick, setSavedTick] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setError(null);
    getSpec(workspaceId, specId)
      .then((spec) => {
        if (cancelled) return;
        setName(spec.name);
        setYamlContent(spec.yaml_content);
      })
      .catch((err) => {
        if (!cancelled) setError(formatGraphApiError(err));
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return (): void => {
      cancelled = true;
    };
  }, [workspaceId, specId]);

  const handleSave = (): void => {
    setIsSaving(true);
    setSavedTick(false);
    setError(null);
    updateSpec(workspaceId, specId, yamlContent)
      .then((saved) => {
        // Backend returns the canonical YAML — mirror it back into the editor.
        setName(saved.name);
        setYamlContent(saved.yaml_content);
        setSavedTick(true);
      })
      .catch((err) => setError(formatGraphApiError(err)))
      .finally(() => setIsSaving(false));
  };

  const handleRun = (): void => {
    setIsRunning(true);
    setError(null);
    runGraph(workspaceId, specId, userInput.trim() || undefined)
      .then((resp) => onRun(resp.graph_instance_id))
      .catch((err) => {
        setError(formatGraphApiError(err));
        setIsRunning(false);
      });
  };

  return (
    <div className="flex-1 overflow-y-auto px-6 py-6">
      <div className="mx-auto flex max-w-3xl flex-col gap-4">
        <div className="flex items-center justify-between">
          <Button variant="ghost" size="sm" onClick={onBack} className="gap-1.5 -ml-2">
            <ArrowLeft size={14} />
            {t("graphs.back")}
          </Button>
          {name ? <span className="text-base font-medium text-ink">{name}</span> : null}
        </div>

        {isLoading ? (
          <p className="text-base text-mute">{t("graphs.loading")}</p>
        ) : (
          <>
            <Textarea
              label={t("graphs.specYamlLabel")}
              value={yamlContent}
              onChange={(e): void => {
                setYamlContent(e.target.value);
                setSavedTick(false);
              }}
              rows={24}
              spellCheck={false}
              className="leading-relaxed"
            />
            {error ? (
              <pre className="whitespace-pre-wrap rounded-sm border border-danger bg-canvas-elevated px-3 py-2 font-mono text-xs text-danger">
                {error}
              </pre>
            ) : null}
            <Input
              label={t("graphs.userInputLabel")}
              value={userInput}
              onChange={(e): void => setUserInput(e.target.value)}
              placeholder={t("graphs.userInputPlaceholder")}
            />
            <div className="flex items-center justify-end gap-2">
              {savedTick ? (
                <span className="text-xs text-success">{t("graphs.saved")}</span>
              ) : null}
              <Button
                variant="secondary"
                size="md"
                onClick={handleSave}
                loading={isSaving}
              >
                {isSaving ? t("graphs.saving") : t("graphs.save")}
              </Button>
              <Button
                variant="primary"
                size="md"
                onClick={handleRun}
                loading={isRunning}
              >
                <Play size={14} />
                {t("graphs.run")}
              </Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
};
