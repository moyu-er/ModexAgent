// GraphSpecEditor — full-canvas topology preview with slide-out YAML editor panel.
// The canvas occupies the full area; a header bar has Back + spec name + Edit YAML.
// Clicking "Edit YAML" slides out a right-side panel with CodeMirror + Cancel/Save.

import { useEffect, useState, type FC } from "react";
import { ArrowLeft, Code2 } from "lucide-react";
import { getSpec, updateSpec } from "../../lib/graphsApi";
import { useT } from "../../i18n";
import { Button } from "../ui/Button";
import { formatGraphApiError } from "./shared";
import { YamlCodeEditor } from "./yaml/YamlCodeEditor";
import {
  GraphSpecParseError,
  parseGraphSpecYaml,
  type ParsedGraphTopology,
} from "./yaml/parseGraphSpec";
import { TopologyCanvas } from "./topology/TopologyCanvas";
import { ApiError } from "../../lib/api";

export interface GraphSpecEditorProps {
  workspaceId: string;
  specId: string;
  onBack: () => void;
  /** ADR-0040: content change on save yields a new spec_id; host navigates. */
  onSpecIdChanged?: (newSpecId: string) => void;
}

const PREVIEW_DEBOUNCE_MS = 300;

function extractBackendLineErrors(
  err: unknown,
): Array<{ line: number; message: string }> {
  if (!(err instanceof ApiError) || !err.detail) return [];
  let body: { error?: string; detail?: unknown };
  try {
    body = JSON.parse(err.detail);
  } catch {
    return [];
  }
  const errorText = body.error ?? "";
  const detailText = typeof body.detail === "string" ? body.detail : "";
  const fullText = detailText ? `${errorText}: ${detailText}` : errorText;
  const match = fullText.match(/line\s+(\d+)/i);
  if (match?.[1]) {
    const line = parseInt(match[1], 10);
    if (!Number.isNaN(line) && line > 0) {
      return [{ line, message: fullText }];
    }
  }
  return [];
}

export const GraphSpecEditor: FC<GraphSpecEditorProps> = ({
  workspaceId,
  specId,
  onBack,
  onSpecIdChanged,
}) => {
  const t = useT();
  const [name, setName] = useState("");
  const [yamlContent, setYamlContent] = useState("");
  const [panelYaml, setPanelYaml] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lintErrors, setLintErrors] = useState<
    Array<{ line: number; message: string }>
  >([]);
  const [previewTopology, setPreviewTopology] =
    useState<ParsedGraphTopology | null>(null);
  const [parseError, setParseError] = useState<GraphSpecParseError | null>(
    null,
  );

  // Load spec
  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setError(null);
    setLintErrors([]);
    getSpec(workspaceId, specId)
      .then((spec) => {
        if (cancelled) return;
        setName(spec.name);
        setYamlContent(spec.yaml_content);
        try {
          setPreviewTopology(parseGraphSpecYaml(spec.yaml_content));
          setParseError(null);
        } catch (e) {
          if (e instanceof GraphSpecParseError) setParseError(e);
        }
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

  // Debounced live preview from yamlContent (the canonical source)
  useEffect(() => {
    if (isLoading) return;
    const timer = setTimeout(() => {
      try {
        setPreviewTopology(parseGraphSpecYaml(yamlContent));
        setParseError(null);
      } catch (e) {
        if (e instanceof GraphSpecParseError) setParseError(e);
      }
    }, PREVIEW_DEBOUNCE_MS);
    return (): void => clearTimeout(timer);
  }, [yamlContent, isLoading]);

  // Open YAML panel — initialize editor content from current canonical yamlContent
  const openPanel = (): void => {
    setPanelYaml(yamlContent);
    setLintErrors([]);
    setError(null);
    setPanelOpen(true);
  };

  const handleSave = (): void => {
    setIsSaving(true);
    setError(null);
    setLintErrors([]);
    updateSpec(workspaceId, specId, panelYaml)
      .then((saved) => {
        setName(saved.name);
        setYamlContent(saved.yaml_content);
        setPanelOpen(false);
        try {
          setPreviewTopology(parseGraphSpecYaml(saved.yaml_content));
          setParseError(null);
        } catch {
          // Backend validated; ignore.
        }
        if (saved.spec_id !== specId) {
          onSpecIdChanged?.(saved.spec_id);
        }
      })
      .catch((err) => {
        setError(formatGraphApiError(err));
        const lineErrs = extractBackendLineErrors(err);
        if (lineErrs.length > 0) setLintErrors(lineErrs);
      })
      .finally(() => setIsSaving(false));
  };

  if (isLoading) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <p className="text-base text-mute">{t("graphs.loading")}</p>
      </div>
    );
  }

  return (
    <div className="flex flex-1 flex-col min-h-0" data-testid="spec-editor">
      {/* Header bar */}
      <div className="flex items-center justify-between border-b border-hairline px-4 py-2.5">
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={onBack}
            className="gap-1.5 -ml-1.5"
          >
            <ArrowLeft size={14} />
            {t("graphs.back")}
          </Button>
          {name ? (
            <span className="text-base font-medium text-ink">{name}</span>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            size="sm"
            onClick={openPanel}
            className="gap-1.5"
            data-testid="spec-editor-edit-yaml"
          >
            <Code2 size={14} />
            {t("graphs.editYaml")}
          </Button>
        </div>
      </div>

      {/* Error banner (load/save errors) */}
      {error && !panelOpen ? (
        <pre className="mx-4 mt-3 whitespace-pre-wrap rounded-sm border border-danger bg-canvas-elevated px-3 py-2 font-mono text-xs text-danger">
          {error}
        </pre>
      ) : null}

      {/* Full-canvas topology preview */}
      <div className="relative flex flex-1 min-h-0">
        {previewTopology ? (
          <TopologyCanvas
            topology={previewTopology}
            className="flex-1"
          />
        ) : parseError ? (
          <div className="flex flex-1 items-center justify-center">
            <p className="text-sm text-danger" data-testid="spec-editor-parse-error">
              {t("graphs.parseError")}: {parseError.message}
            </p>
          </div>
        ) : (
          <div className="flex flex-1 items-center justify-center">
            <p className="text-base text-mute">{t("graphs.loading")}</p>
          </div>
        )}
        {parseError && previewTopology ? (
          <div className="absolute bottom-3 left-3 max-w-md rounded-sm border border-danger bg-canvas-elevated px-3 py-1.5 font-mono text-xs text-danger">
            {t("graphs.parseError")}: {parseError.message}
          </div>
        ) : null}
      </div>

      {/* YAML slide-out panel */}
      {panelOpen ? (
        <>
          {/* Scrim */}
          <div
            className="fixed inset-0 z-40 bg-overlay modal-scrim-enter"
            onClick={() => setPanelOpen(false)}
            aria-hidden="true"
            data-testid="spec-editor-scrim"
          />
          {/* Panel */}
          <div
            className="slide-in-right fixed right-0 top-0 bottom-0 z-50 flex w-full flex-col border-l border-hairline bg-canvas-popover shadow-popover md:w-[480px]"
            role="dialog"
            aria-label={t("graphs.yamlConfig")}
            data-testid="spec-editor-panel"
          >
            {/* Panel header */}
            <div className="flex items-center justify-between border-b border-hairline px-4 py-3">
              <span className="font-mono text-sm font-medium text-ink">
                {t("graphs.yamlConfig")}
              </span>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setPanelOpen(false)}
                className="-mr-2"
              >
                {t("graphs.back")}
              </Button>
            </div>

            {/* CodeMirror editor */}
            <div className="flex-1 min-h-0 p-3">
              <YamlCodeEditor
                value={panelYaml}
                onChange={(v): void => setPanelYaml(v)}
                errors={lintErrors}
                className="h-full"
              />
            </div>

            {/* Error panel (conditional) */}
            {error ? (
              <div
                className="border-t border-hairline px-4 py-2"
                data-testid="spec-editor-error-panel"
              >
                <pre className="whitespace-pre-wrap font-mono text-xs text-danger">
                  {error}
                </pre>
              </div>
            ) : null}

            {/* Footer: Cancel / Save */}
            <div className="flex items-center justify-end gap-2 border-t border-hairline px-4 py-2.5">
              <Button
                variant="ghost"
                size="md"
                onClick={() => setPanelOpen(false)}
                disabled={isSaving}
              >
                {t("common.cancel")}
              </Button>
              <Button
                variant="primary"
                size="md"
                onClick={handleSave}
                loading={isSaving}
              >
                {isSaving ? t("graphs.saving") : t("graphs.save")}
              </Button>
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
};
