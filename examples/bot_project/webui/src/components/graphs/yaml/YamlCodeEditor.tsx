/**
 * YamlCodeEditor.tsx — CodeMirror 6 封装(graph PRD §6.2 A 区)。
 *
 * - CodeMirror 模块全部动态 import(lazy),Vite 单独拆 chunk,不影响 chat
 *   首屏 bundle。
 * - YAML 语法高亮(@codemirror/lang-yaml)+ 行号 + 活跃行高亮。
 * - 自定义 Teal & Ember 主题:背景/文字/注释/关键字映射到 --color-* token,
 *   随 .dark 自动切换。
 * - 受控组件:外部传 value/onChange;内部变更通过 updateListener 回调外部。
 * - 外部 lint:errors prop → setDiagnostics → gutter marker + 波浪下划线。
 *   用户编辑后 linter 自动清空旧诊断(默认 debounce),Save 后重新注入。
 * - 加载中:占位 div;加载失败:textarea 降级。
 */
import { useEffect, useRef, useState, type FC } from "react";
import { useT } from "../../../i18n";

// Type-only imports — erased at compile time, zero bundle impact.
// Runtime values come from dynamic import() inside useEffect.
import type { EditorView, ViewUpdate } from "@codemirror/view";

// ── Types ───────────────────────────────────────────────────────────────────

export interface YamlCodeEditorProps {
  value: string;
  onChange?: (value: string) => void;
  errors?: ReadonlyArray<{ line: number; message: string }>;
  className?: string;
}

/** CodeMirror Diagnostic 兼容结构(纯函数 mapErrorsToDiagnostics 产出)。 */
export interface EditorDiagnostic {
  from: number;
  to: number;
  severity: "error";
  message: string;
}

// ── Pure functions (exported for testing) ───────────────────────────────────

/**
 * 将 { line, message } 错误列表转换为 CodeMirror Diagnostic 兼容结构。
 * `from`/`to` 是文档内的 0-based 字符偏移;line 是 1-based。
 * 越界行号 clamp 到最后一行,不会抛异常。
 */
export function mapErrorsToDiagnostics(
  errors: ReadonlyArray<{ line: number; message: string }>,
  source: string,
): EditorDiagnostic[] {
  if (errors.length === 0) return [];
  const lines = source.split("\n");
  // Precompute line start offsets for O(n) lookup.
  const lineStarts: number[] = [0];
  for (let i = 0; i < lines.length; i++) {
    lineStarts.push((lineStarts[i] ?? 0) + (lines[i]?.length ?? 0) + 1); // +1 for \n
  }
  return errors.map(({ line, message }) => {
    const idx = Math.max(0, Math.min(line - 1, lines.length - 1));
    const from = lineStarts[idx] ?? 0;
    const lineText = lines[idx] ?? "";
    return {
      from,
      to: from + lineText.length,
      severity: "error",
      message,
    } satisfies EditorDiagnostic;
  });
}

// ── Theme spec (CSS variable references, resolved at runtime) ───────────────

/**
 * EditorView.theme() spec — 所有颜色引用 --color-* token,随 .dark 切换。
 * 不使用 CodeMirror 的 dark 选项;主题完全由 CSS 变量驱动。
 */
const THEME_SPEC: Record<string, Record<string, string>> = {
  "&": {
    backgroundColor: "var(--color-canvas-elevated)",
    color: "var(--color-ink)",
    height: "100%",
    fontSize: "13px",
  },
  ".cm-scroller": {
    fontFamily: "'JetBrains Mono', 'Courier New', monospace",
    lineHeight: "1.6",
    overflow: "auto",
  },
  ".cm-content": {
    fontFamily: "'JetBrains Mono', 'Courier New', monospace",
    padding: "8px 0",
  },
  ".cm-gutters": {
    backgroundColor: "var(--color-canvas-elevated)",
    color: "var(--color-faint)",
    border: "none",
    borderRight: "1px solid var(--color-hairline)",
  },
  ".cm-lineNumbers .cm-gutterElement": {
    color: "var(--color-faint)",
    fontFamily: "'JetBrains Mono', 'Courier New', monospace",
    fontSize: "12px",
    padding: "0 8px",
  },
  ".cm-activeLine": {
    backgroundColor: "var(--color-hairline-soft)",
  },
  ".cm-activeLineGutter": {
    backgroundColor: "var(--color-hairline-soft)",
  },
  "&.cm-focused": {
    outline: "none",
  },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground": {
    backgroundColor: "var(--color-selection)",
  },
  // Lint markers
  ".cm-lintRange-error": {
    textDecoration: "underline wavy var(--color-danger)",
    textDecorationThickness: "1.5px",
  },
  ".cm-lint-marker-error": {
    color: "var(--color-danger)",
    backgroundColor: "var(--color-danger)",
  },
  ".cm-diagnosticText": {
    fontFamily: "'JetBrains Mono', 'Courier New', monospace",
    fontSize: "12px",
  },
  ".cm-diagnostic-error": {
    borderLeft: "2px solid var(--color-danger)",
  },
};

// ── Component ───────────────────────────────────────────────────────────────

export const YamlCodeEditor: FC<YamlCodeEditorProps> = ({
  value,
  onChange,
  errors,
  className,
}) => {
  const t = useT();
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  /** setDiagnostics 函数(从动态 import 获得);存在 ref 避免重建 editor。 */
  const applyDiagnosticsRef = useRef<
    ((diags: EditorDiagnostic[]) => void) | null
  >(null);

  // Refs for the latest prop values — the dynamic-import callback and the
  // updateListener closure read from these, so they always see current props
  // without needing to rebuild the editor.
  const onChangeRef = useRef(onChange);
  const valueRef = useRef(value);
  const errorsRef = useRef(errors);
  onChangeRef.current = onChange;
  valueRef.current = value;
  errorsRef.current = errors;

  const [status, setStatus] = useState<"loading" | "ready" | "error">(
    "loading",
  );
  const [errorMsg, setErrorMsg] = useState<string>("");

  // ── Initialize CodeMirror (lazy import, runs once) ──────────────────────

  useEffect(() => {
    let cancelled = false;
    let view: EditorView | null = null;

    Promise.all([
      import("@codemirror/state"),
      import("@codemirror/view"),
      import("@codemirror/lang-yaml"),
      import("@codemirror/lint"),
      import("@codemirror/language"),
      import("@lezer/highlight"),
    ])
      .then(
        ([
          { EditorState },
          { EditorView: EV, lineNumbers, highlightActiveLine },
          { yaml },
          { linter, lintGutter, setDiagnostics },
          { HighlightStyle, syntaxHighlighting },
          { tags },
        ]) => {
          if (cancelled || !hostRef.current) return;

          const highlightStyle = HighlightStyle.define([
            { tag: tags.comment, color: "var(--color-mute)" },
            { tag: tags.keyword, color: "var(--color-brand)" },
            { tag: tags.string, color: "var(--color-brand-bright)" },
            { tag: tags.number, color: "var(--color-ember)" },
            { tag: tags.bool, color: "var(--color-brand)" },
          ]);

          const extensions = [
            lineNumbers(),
            highlightActiveLine(),
            yaml(),
            syntaxHighlighting(highlightStyle),
            lintGutter(),
            // No-op linter: enables lint state field so setDiagnostics works.
            // Returns [] on document changes, clearing stale Save-time errors
            // after the user starts editing (debounced ~750ms by default).
            linter(() => []),
            EV.theme(THEME_SPEC),
            EV.lineWrapping,
            EV.updateListener.of((update: ViewUpdate) => {
              if (update.docChanged) {
                onChangeRef.current?.(update.state.doc.toString());
              }
            }),
          ];

          const state = EditorState.create({
            doc: valueRef.current,
            extensions,
          });

          view = new EV({ state, parent: hostRef.current });
          viewRef.current = view;
          applyDiagnosticsRef.current = (diags: EditorDiagnostic[]) => {
            view?.dispatch(setDiagnostics(view.state, diags));
          };

          // Apply initial errors if any were provided before the editor loaded.
          const currentErrors = errorsRef.current;
          if (currentErrors && currentErrors.length > 0) {
            const diags = mapErrorsToDiagnostics(
              currentErrors,
              valueRef.current,
            );
            view.dispatch(setDiagnostics(view.state, diags));
          }

          setStatus("ready");
        },
      )
      .catch((err: unknown) => {
        if (cancelled) return;
        setErrorMsg(err instanceof Error ? err.message : String(err));
        setStatus("error");
      });

    return () => {
      cancelled = true;
      view?.destroy();
      viewRef.current = null;
      applyDiagnosticsRef.current = null;
    };
  }, []);

  // ── Sync external value → editor ───────────────────────────────────────

  useEffect(() => {
    if (status !== "ready") return;
    const view = viewRef.current;
    if (!view) return;
    const currentDoc = view.state.doc.toString();
    if (currentDoc !== value) {
      view.dispatch({
        changes: { from: 0, to: currentDoc.length, insert: value },
      });
    }
  }, [value, status]);

  // ── Sync external errors → editor lint ─────────────────────────────────

  useEffect(() => {
    if (status !== "ready") return;
    const applyDiags = applyDiagnosticsRef.current;
    if (!applyDiags) return;
    const diags = mapErrorsToDiagnostics(errors ?? [], value);
    applyDiags(diags);
  }, [errors, status, value]);

  // ── Render ─────────────────────────────────────────────────────────────

  return (
    <div
      className={`relative h-full min-h-[300px] overflow-hidden rounded-sm border border-hairline bg-canvas-elevated ${className ?? ""}`}
      data-testid="yaml-editor-host"
    >
      <div
        ref={hostRef}
        className="h-full overflow-auto"
        data-testid="yaml-editor-container"
      />
      {status === "loading" ? (
        <div
          className="absolute inset-0 flex items-center justify-center bg-canvas-elevated"
          data-testid="yaml-editor-loading"
        >
          <span className="font-mono text-xs text-faint">{t("graphs.loadingEditor")}</span>
        </div>
      ) : null}
      {status === "error" ? (
        <>
          <textarea
            value={value}
            onChange={(e): void => onChange?.(e.target.value)}
            spellCheck={false}
            className="absolute inset-0 h-full w-full resize-none border-none bg-canvas-elevated p-3 font-mono text-xs text-ink focus:outline-none"
            data-testid="yaml-editor-fallback"
            aria-label={t("graphs.yamlEditorFallback")}
          />
          {errorMsg ? (
            <div
              className="absolute bottom-0 left-0 right-0 bg-danger/10 px-3 py-1 font-mono text-xs text-danger"
              data-testid="yaml-editor-error"
            >
              {errorMsg}
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
};
