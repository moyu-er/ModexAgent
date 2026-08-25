// ScopeView — 声明 scope 树画布 + provenance 账单 + 声明 YAML 写回
// (票据16;SPEC §3.4 账单按请求重算无缓存)。
//
// 三段:声明树画布(复用共享 TopologyCanvas — ADR-0043 同形不合并,
// 映射在 ./scopeTopology.ts)/ 账单(ScopeBillView)/ YAML 编辑器
// (YamlCodeEditor,写回循 PoolEditor 模式:PUT 写 config/scopes/bot.yml,
// 重启生效提示 restartToast)。保存后重拉拓扑+账单——未重启的编辑立即
// 在账单中显示为磁盘声明(S2)。

import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError } from "../../lib/api";
import {
  getScopeBill,
  getScopeDeclaration,
  getScopeTopology,
  saveScopeDeclaration,
  type ScopeAgentBill,
  type ScopeTopology,
} from "../../lib/scopeApi";
import { useToast } from "../ToastContext";
import { TopologyCanvas } from "../graphs/topology/TopologyCanvas";
import { YamlCodeEditor } from "../graphs/yaml/YamlCodeEditor";
import { ActionBar } from "../ui/ActionBar";
import { Button } from "../ui/Button";
import { SectionLabel } from "../ui/SectionLabel";
import { CATEGORY } from "./categoryMeta";
import { restartToast } from "./restartToast";
import { ScopeBillView } from "./ScopeBillView";
import { scopeTopologyToCanvas } from "./scopeTopology";
import { useT, type TFn } from "../../i18n";

function formatSaveError(e: unknown, t: TFn): string {
  if (e instanceof ApiError) {
    try {
      const body = JSON.parse(e.detail) as {
        error?: string;
        detail?: unknown;
        issues?: { rule: string; node: string; message: string }[];
      };
      if (body.issues && body.issues.length > 0) {
        return t("settings.scope.saveFailed", {
          detail: body.issues
            .map((i) => `${i.rule} ${i.node}: ${i.message}`)
            .join("; "),
        });
      }
      if (body.detail) {
        return t("settings.scope.saveFailed", {
          detail:
            typeof body.detail === "string"
              ? body.detail
              : JSON.stringify(body.detail),
        });
      }
      if (body.error) {
        return t("settings.scope.saveFailed", { detail: body.error });
      }
    } catch {
      // detail 非 JSON — 落到通用分支
    }
    return t("settings.scope.saveFailed", { detail: `${e.status} ${e.detail}` });
  }
  return t("settings.scope.saveFailed", { detail: String(e) });
}

export function ScopeView() {
  const toast = useToast();
  const t = useT();
  const [yaml, setYaml] = useState<string | null>(null);
  const [original, setOriginal] = useState<string>("");
  const [topology, setTopology] = useState<ScopeTopology | null>(null);
  const [bill, setBill] = useState<ScopeAgentBill[] | null>(null);
  const [loadError, setLoadError] = useState<string>("");
  const [saveError, setSaveError] = useState<string>("");
  const [saving, setSaving] = useState<boolean>(false);

  const load = useCallback(async (): Promise<void> => {
    setLoadError("");
    const [decl, topo, agents] = await Promise.all([
      getScopeDeclaration(),
      getScopeTopology(),
      getScopeBill(),
    ]);
    setYaml(decl);
    setOriginal(decl);
    setTopology(topo);
    setBill(agents);
  }, []);

  useEffect(() => {
    void load().catch((e: unknown) =>
      setLoadError(t("common.failedToLoad", { error: String(e) })),
    );
  }, [load, t]);

  const canvasTopology = useMemo(
    () => (topology ? scopeTopologyToCanvas(topology) : null),
    [topology],
  );
  const dirty = yaml !== null && yaml !== original;

  const save = async (): Promise<void> => {
    if (yaml === null) return;
    setSaving(true);
    setSaveError("");
    try {
      await saveScopeDeclaration(yaml);
      // 写回后账单/拓扑按请求从磁盘重算——立即反映未重启的编辑。
      const [topo, agents] = await Promise.all([getScopeTopology(), getScopeBill()]);
      setOriginal(yaml);
      setTopology(topo);
      setBill(agents);
      restartToast(toast, t);
    } catch (e) {
      setSaveError(formatSaveError(e, t));
    } finally {
      setSaving(false);
    }
  };

  const cancel = (): void => {
    setYaml(original);
    setSaveError("");
  };

  const meta = CATEGORY.scope;
  const PageHeadIcon = meta.icon;

  if (loadError) {
    return <p className="text-base text-error">{loadError}</p>;
  }
  if (yaml === null || topology === null || bill === null) {
    return <p className="text-base text-mute">{t("common.loading")}</p>;
  }

  return (
    <div data-testid="scope-view" className="space-y-6">
      <div className="page-head">
        <span
          className="page-head-icon"
          style={{ ["--cat" as string]: meta.catVar }}
        >
          <PageHeadIcon size={18} />
        </span>
        <div>
          <div className="page-title">{t(meta.titleKey!)}</div>
          <div className="page-sub">{t(meta.subKey)}</div>
        </div>
      </div>

      <section>
        <SectionLabel>{t("settings.scope.tree")}</SectionLabel>
        <div className="mt-2 h-[420px] overflow-hidden rounded-lg border border-hairline">
          {canvasTopology ? (
            <TopologyCanvas topology={canvasTopology} className="h-full" />
          ) : null}
        </div>
      </section>

      <section>
        <SectionLabel>{t("settings.scope.bill")}</SectionLabel>
        <div className="mt-2">
          <ScopeBillView agents={bill} />
        </div>
      </section>

      <section>
        <SectionLabel>{t("settings.scope.editor")}</SectionLabel>
        {saveError ? (
          <pre
            data-testid="scope-save-error"
            className="mt-2 whitespace-pre-wrap rounded-sm border border-danger bg-canvas-elevated px-3 py-2 font-mono text-xs text-danger"
          >
            {saveError}
          </pre>
        ) : null}
        <div
          data-testid="scope-declaration-editor"
          className="mt-2 overflow-hidden rounded-lg border border-hairline"
        >
          <YamlCodeEditor
            value={yaml}
            onChange={setYaml}
            className="h-[360px]"
          />
        </div>
        <ActionBar dirty={dirty}>
          <Button
            variant="secondary"
            size="sm"
            onClick={cancel}
            disabled={!dirty || saving}
          >
            {t("common.cancel")}
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={() => void save()}
            disabled={!dirty || saving}
            loading={saving}
            data-testid="scope-save"
          >
            {t("common.save")}
          </Button>
        </ActionBar>
      </section>
    </div>
  );
}
