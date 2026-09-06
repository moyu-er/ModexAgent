// AgentForm.tsx — the pools panel's per-agent form, v2 (PRD Part C,
// effective-state-driven). Composition sections render the EFFECTIVE state
// from the bill (disk bill when clean, debounced /api/scope/preview when
// dirty — the parent owns that loop); edits write declared deviations into
// the draft model. Structure:
//
//   运行时   RuntimeSection (C3) — strategy-first; external replaces every
//          native section with the provider panel (identity-only form).
//   基本     description / max_steps / root terminal flags.
//   能力     CapabilityRow bundle rows (C1) — the primary composition face.
//   工具     toolset dropdown + read-only effective tool roster + MCP.
//   Hooks    effective roster with provenance badges + veto/restore + add
//            combobox (C2); dangling vetoes surfaced for cleanup.
//   权限     (root only) interceptors effective roster + sandbox_guard config
//            + approval + apply-to-other-pools.
//   高级     context_mode / fork_max / eager / memory (root) / prompt_name.

import { Ban, RotateCcw, X } from "lucide-react";
import type {
  ScopeAgentBill,
  ScopeModelIssue,
  ScopeOptions,
} from "../../../lib/scopeApi";
import { useT } from "../../../i18n";
import { Button } from "../../ui/Button";
import { Checkbox } from "../../ui/Checkbox";
import { DropdownPanel } from "../../ui/DropdownPanel";
import { Input } from "../../ui/Input";
import { Textarea } from "../../ui/Textarea";
import { HelperText } from "../../ui/HelperText";
import { FormSection } from "./FormSection";
import { RuntimeSection } from "./RuntimeSection";
import { CapabilityRow } from "./CapabilityRow";
import { RosterCombobox } from "./RosterCombobox";
import { Badge, Chip } from "./chips";
import {
  addDeclaredHook,
  asNumber,
  asString,
  asStringList,
  bundleCarriedHooks,
  capabilityMode,
  declaredInterceptors,
  ensureNested,
  hookCandidates,
  interceptorOn,
  nestedMap,
  removeDeclaredHook,
  restoreHook,
  setCapabilityMode,
  setField,
  setInterceptor,
  toggleInListField,
  vetoedHooks,
  vetoHook,
  type AgentBody,
  type AgentTreeNode,
  type CapabilityMode,
} from "./scopeModel";

const SANDBOX_BACKENDS = ["host", "auto", "local", "oci"] as const;
const WRITE_SURFACES = ["workspace", "roots", "none", "full"] as const;
const APPROVAL_TOOLS = ["write", "edit", "bash"] as const;
const MAX_STEPS_DEFAULT = 100;
const FORK_MAX_DEFAULT = 80;
const FORK_MAX_LIMIT = 100;

interface Props {
  pool: string;
  node: AgentTreeNode;
  options: ScopeOptions;
  prompts: string[];
  issues: ScopeModelIssue[];
  /** The effective bill entry for this agent (disk bill or live preview). */
  bill: ScopeAgentBill | null;
  /** Apply a mutation to this agent's body inside the draft model. */
  updateAgent: (mut: (body: AgentBody) => void) => void;
  onApplyToPools: () => void;
}

export function AgentForm({
  pool,
  node,
  options,
  prompts,
  issues,
  bill,
  updateAgent,
  onApplyToPools,
}: Props) {
  const t = useT();
  const body = node.body;
  const isRoot = node.path.length === 1;
  const position = isRoot ? options.position_defaults.root : options.position_defaults.sub;
  const executionStrategy = asString(body.execution_strategy) || "react";
  const isExternal = executionStrategy === "external";
  const contextMode = asString(body.context_mode);

  const declaredCaps = Object.keys(nestedMap(body, "capabilities") ?? {});
  const capabilityNames = [
    ...options.capabilities,
    ...declaredCaps.filter((n) => !options.capabilities.includes(n)),
  ];
  const mcpNames = [
    ...options.mcp_servers,
    ...asStringList(body.mcp).filter((n) => !options.mcp_servers.includes(n)),
  ];

  const effectiveHooks = new Set((bill?.hooks ?? []).map((h) => h.hook));
  const enabledBundleHooks = new Set<string>(
    (bill?.capabilities ?? [])
      .filter((c) => c.state !== "vetoed")
      .flatMap((c) => options.capability_bundles[c.capability]?.hooks ?? []),
  );
  const addHookCandidates = hookCandidates(
    options.hooks,
    bundleCarriedHooks(options.capability_bundles),
    effectiveHooks,
  );

  const interceptors = declaredInterceptors(body);
  const sandboxGuardOn = interceptorOn(body, "sandbox_guard");
  const sandboxBackend =
    asString(nestedMap(body, "interceptor_configs", "sandbox_guard", "sandbox")?.backend) ||
    "host";
  const writeSurface =
    asString(
      nestedMap(body, "interceptor_configs", "sandbox_guard", "sandbox", "exclusive")
        ?.write_surface,
    ) || "workspace";
  const approval = nestedMap(body, "approval");
  const approvalEnabled = approval?.enabled === true;

  const memory = nestedMap(body, "memory");
  const archiveOn = memory?.archive_enabled === true;
  const coreOn = memory?.core_enabled === true;

  return (
    <div className="space-y-4" data-testid="pools-agent-form">
      <h3 className="font-mono text-base font-semibold text-bright">
        {isRoot
          ? t("settings.poolsPanel.agentHeadingRoot", { pool, name: node.name })
          : t("settings.poolsPanel.agentHeadingSub", { pool, name: node.name })}
      </h3>

      {issues.length > 0 ? (
        <ul
          data-testid="pools-node-issues"
          className="space-y-1 rounded-sm border border-danger bg-canvas-elevated px-3 py-2"
        >
          {issues.map((issue, i) => (
            <li key={`${issue.rule}-${i}`} className="text-xs text-danger">
              <span className="font-mono font-semibold">{issue.rule}</span> {issue.message}
            </li>
          ))}
        </ul>
      ) : null}

      <RuntimeSection
        strategy={asString(body.execution_strategy)}
        providerKind={asString(body.provider_kind)}
        options={options}
        onStrategyChange={(v) =>
          updateAgent((b) => {
            setField(b, "execution_strategy", v || null);
            if (v !== "external") delete b.provider_kind;
          })
        }
        onProviderKindChange={(v) =>
          updateAgent((b) => setField(b, "provider_kind", v || null))
        }
      />

      <FormSection title={t("settings.poolsPanel.sectionBasic")}>
        <Textarea
          label={t("settings.poolsPanel.description")}
          helper={t("settings.poolsPanel.descriptionHelper")}
          mono={false}
          value={asString(body.description)}
          onChange={(e) =>
            updateAgent((b) => setField(b, "description", e.target.value || null))
          }
        />
        {!isExternal ? (
          <>
            <Input
              type="number"
              min={1}
              label={t("settings.poolsPanel.maxSteps")}
              helper={t("settings.poolsPanel.maxStepsHelper", { default: MAX_STEPS_DEFAULT })}
              value={asNumber(body.max_steps)?.toString() ?? ""}
              onChange={(e) => {
                const raw = e.target.value;
                updateAgent((b) =>
                  setField(b, "max_steps", raw === "" ? null : Math.max(1, Number(raw))),
                );
              }}
            />
            {isRoot ? (
              <div className="space-y-2">
                <Checkbox
                  label={t("settings.poolsPanel.useTerminal")}
                  checked={body.use_terminal === true}
                  onChange={(e) =>
                    updateAgent((b) => setField(b, "use_terminal", e.target.checked))
                  }
                />
                <Checkbox
                  label={t("settings.poolsPanel.terminalVisibility")}
                  checked={body.terminal_visibility === true}
                  onChange={(e) =>
                    updateAgent((b) => setField(b, "terminal_visibility", e.target.checked))
                  }
                />
              </div>
            ) : null}
          </>
        ) : null}
      </FormSection>

      {isExternal ? null : (
        <>
          <FormSection title={t("settings.poolsPanel.sectionCapabilities")}>
            <HelperText>{t("settings.poolsPanel.capabilitiesHelper")}</HelperText>
            <div className="space-y-2">
              {capabilityNames.map((name) => (
                <CapabilityRow
                  key={name}
                  name={name}
                  bill={bill?.capabilities.find((c) => c.capability === name) ?? null}
                  bundle={options.capability_bundles[name] ?? null}
                  isRoot={isRoot}
                  mode={capabilityMode(body, name)}
                  onModeChange={(mode: CapabilityMode) =>
                    updateAgent((b) => setCapabilityMode(b, name, mode))
                  }
                />
              ))}
            </div>
          </FormSection>

          <FormSection title={t("settings.poolsPanel.sectionTools")}>
            <DropdownPanel
              label={t("settings.poolsPanel.toolset")}
              helper={t("settings.poolsPanel.toolsetHelper", { value: position.toolset })}
              value={asString(body.toolset)}
              options={[
                { value: "", label: t("settings.poolsPanel.positionDefault") },
                ...options.toolsets.map((ts) => ({ value: ts, label: ts })),
              ]}
              onChange={(v) => updateAgent((b) => setField(b, "toolset", v || null))}
            />
            <div>
              <span className="mb-1 block text-base text-ink">
                {t("settings.poolsPanel.effectiveTools")}
              </span>
              {bill === null ? (
                <HelperText>{t("settings.poolsPanel.effectiveLoading")}</HelperText>
              ) : (
                <div className="mt-2 flex flex-wrap gap-1.5" data-testid="pools-effective-tools">
                  {bill.tools.map((tool) => (
                    <Chip
                      key={tool.tool}
                      title={t("settings.poolsPanel.originTitle", { origin: tool.origin })}
                    >
                      {tool.tool}
                      {tool.capability ? (
                        <Badge tone="brand">
                          {t("settings.poolsPanel.capabilityBadge", { name: tool.capability })}
                        </Badge>
                      ) : null}
                    </Chip>
                  ))}
                </div>
              )}
              {bill !== null && bill.replacements.length > 0 ? (
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  <span className="text-xs text-mute">
                    {t("settings.poolsPanel.replacements")}
                  </span>
                  {bill.replacements.map((r) => (
                    <Chip
                      key={`${r.default_tool}-${r.replacement_tool}`}
                      title={t("settings.poolsPanel.replacementTitle", {
                        supplement: r.supplement,
                      })}
                    >
                      {r.default_tool} ← {r.replacement_tool}
                    </Chip>
                  ))}
                </div>
              ) : null}
            </div>
            <div>
              <span className="mb-1 block text-base text-ink">
                {t("settings.poolsPanel.mcp")}
              </span>
              {mcpNames.length === 0 ? (
                <HelperText>{t("settings.poolsPanel.mcpEmpty")}</HelperText>
              ) : (
                <div className="mt-2 grid grid-cols-2 gap-2">
                  {mcpNames.map((name) => (
                    <Checkbox
                      key={name}
                      label={name}
                      checked={asStringList(body.mcp).includes(name)}
                      onChange={(e) =>
                        updateAgent((b) => toggleInListField(b, "mcp", name, e.target.checked))
                      }
                    />
                  ))}
                </div>
              )}
            </div>
          </FormSection>

          <FormSection title={t("settings.poolsPanel.sectionHooks")}>
            {bill === null ? (
              <HelperText>{t("settings.poolsPanel.effectiveLoading")}</HelperText>
            ) : (
              <div className="flex flex-wrap gap-1.5" data-testid="pools-effective-hooks">
                {bill.hooks.map((hook) => (
                  <Chip
                    key={hook.hook}
                    title={t("settings.poolsPanel.originTitle", { origin: hook.origin })}
                    actionLabel={
                      hook.origin === "local_hooks"
                        ? t("settings.poolsPanel.removeHook", { name: hook.hook })
                        : t("settings.poolsPanel.vetoHook", { name: hook.hook })
                    }
                    actionIcon={hook.origin === "local_hooks" ? <X size={11} /> : <Ban size={11} />}
                    onAction={() =>
                      updateAgent((b) =>
                        hook.origin === "local_hooks"
                          ? removeDeclaredHook(b, hook.hook)
                          : vetoHook(b, hook.hook),
                      )
                    }
                  >
                    {hook.hook}
                    {hook.origin === "position_default" ? (
                      <Badge>{t("settings.poolsPanel.hookBadgeDefault")}</Badge>
                    ) : hook.origin === "capability_derived" && hook.capability ? (
                      <Badge tone="brand">
                        {t("settings.poolsPanel.capabilityBadge", { name: hook.capability })}
                      </Badge>
                    ) : (
                      <Badge>{t("settings.poolsPanel.hookBadgeDeclared")}</Badge>
                    )}
                  </Chip>
                ))}
              </div>
            )}
            {vetoedHooks(body).length > 0 ? (
              <div className="flex flex-wrap items-center gap-1.5" data-testid="pools-vetoed-hooks">
                <span className="text-xs text-mute">{t("settings.poolsPanel.vetoedHooks")}</span>
                {vetoedHooks(body).map((name) => {
                  const restorable =
                    options.default_hooks.includes(name) || enabledBundleHooks.has(name);
                  return (
                    <Chip
                      key={name}
                      struck
                      title={t("settings.poolsPanel.vetoedTitle", { name })}
                      actionLabel={
                        restorable
                          ? t("settings.poolsPanel.restoreHook", { name })
                          : t("settings.poolsPanel.removeVeto", { name })
                      }
                      actionIcon={<RotateCcw size={11} />}
                      onAction={() => updateAgent((b) => restoreHook(b, name))}
                    >
                      {name}
                    </Chip>
                  );
                })}
              </div>
            ) : null}
            <RosterCombobox
              label={t("settings.poolsPanel.addHook")}
              candidates={addHookCandidates}
              emptyText={t("settings.poolsPanel.noHookCandidates")}
              onPick={(name) => updateAgent((b) => addDeclaredHook(b, name))}
            />
          </FormSection>

          {isRoot ? (
            <FormSection title={t("settings.poolsPanel.sectionPermissions")}>
              <div>
                <span className="mb-1 block text-base text-ink">
                  {t("settings.poolsPanel.interceptors")}
                </span>
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  {interceptors.map((name) => (
                    <Chip
                      key={name}
                      actionLabel={t("settings.poolsPanel.removeInterceptor", { name })}
                      actionIcon={<X size={11} />}
                      onAction={() => updateAgent((b) => setInterceptor(b, name, false))}
                    >
                      {name}
                    </Chip>
                  ))}
                  <RosterCombobox
                    label={t("settings.poolsPanel.addInterceptor")}
                    candidates={options.interceptors.filter((i) => !interceptors.includes(i))}
                    emptyText={t("settings.poolsPanel.noInterceptorCandidates")}
                    onPick={(name) => updateAgent((b) => setInterceptor(b, name, true))}
                  />
                </div>
              </div>

              {sandboxGuardOn ? (
                <div className="space-y-4 rounded-md border border-hairline p-3">
                  <DropdownPanel
                    label={t("settings.poolsPanel.sandboxBackend")}
                    value={sandboxBackend}
                    options={SANDBOX_BACKENDS.map((v) => ({ value: v, label: v }))}
                    onChange={(v) =>
                      updateAgent((b) => {
                        ensureNested(b, "interceptor_configs", "sandbox_guard", "sandbox").backend =
                          v;
                      })
                    }
                  />
                  <DropdownPanel
                    label={t("settings.poolsPanel.writeSurface")}
                    value={writeSurface}
                    options={WRITE_SURFACES.map((v) => ({ value: v, label: v }))}
                    onChange={(v) =>
                      updateAgent((b) => {
                        ensureNested(
                          b,
                          "interceptor_configs",
                          "sandbox_guard",
                          "sandbox",
                          "exclusive",
                        ).write_surface = v;
                      })
                    }
                  />
                </div>
              ) : null}

              <div className="space-y-3">
                <Checkbox
                  label={t("settings.poolsPanel.approvalEnabled")}
                  checked={approvalEnabled}
                  onChange={(e) =>
                    updateAgent((b) => {
                      if (e.target.checked) ensureNested(b, "approval").enabled = true;
                      else delete b.approval;
                    })
                  }
                />
                {approvalEnabled
                  ? APPROVAL_TOOLS.map((tool) => {
                      const entry = nestedMap(body, "approval", "tools", tool);
                      const enabled = entry !== null;
                      const paths = entry ? asStringList(entry.allowed_paths) : [];
                      return (
                        <div key={tool} className="rounded-md border border-hairline p-3">
                          <Checkbox
                            label={tool}
                            checked={enabled}
                            onChange={(e) =>
                              updateAgent((b) => {
                                const tools = ensureNested(b, "approval", "tools");
                                if (e.target.checked) tools[tool] = { allowed_paths: ["./*"] };
                                else delete tools[tool];
                              })
                            }
                          />
                          {enabled ? (
                            <Textarea
                              className="mt-2"
                              label={t("settings.poolsPanel.allowedPaths")}
                              helper={t("settings.poolsPanel.allowedPathsHelper")}
                              value={paths.join("\n")}
                              onChange={(e) =>
                                updateAgent((b) => {
                                  ensureNested(b, "approval", "tools", tool).allowed_paths = e
                                    .target.value
                                    .split("\n")
                                    .map((p) => p.trim())
                                    .filter((p) => p.length > 0);
                                })
                              }
                            />
                          ) : null}
                        </div>
                      );
                    })
                  : null}
              </div>

              <div>
                <Button variant="secondary" size="sm" onClick={onApplyToPools}>
                  {t("settings.poolsPanel.applyToPools")}
                </Button>
                <HelperText>{t("settings.poolsPanel.applyToPoolsHelper")}</HelperText>
              </div>
            </FormSection>
          ) : null}

          <FormSection title={t("settings.poolsPanel.sectionAdvanced")}>
            <DropdownPanel
              label={t("settings.poolsPanel.contextMode")}
              helper={t("settings.poolsPanel.contextModeHelper")}
              value={contextMode}
              options={[
                { value: "", label: t("settings.poolsPanel.positionDefaultFresh") },
                ...options.context_modes.map((m) => ({ value: m, label: m })),
              ]}
              onChange={(v) =>
                updateAgent((b) => {
                  setField(b, "context_mode", v || null);
                  if (v !== "fork") delete b.fork_max_messages;
                })
              }
            />
            {contextMode === "fork" ? (
              <Input
                type="number"
                min={1}
                max={FORK_MAX_LIMIT}
                label={t("settings.poolsPanel.forkMax")}
                helper={t("settings.poolsPanel.forkMaxHelper", {
                  max: FORK_MAX_LIMIT,
                  default: FORK_MAX_DEFAULT,
                })}
                value={asNumber(body.fork_max_messages)?.toString() ?? ""}
                onChange={(e) => {
                  const raw = e.target.value;
                  updateAgent((b) =>
                    setField(
                      b,
                      "fork_max_messages",
                      raw === "" ? null : Math.min(FORK_MAX_LIMIT, Math.max(1, Number(raw))),
                    ),
                  );
                }}
              />
            ) : null}

            <DropdownPanel
              label={t("settings.poolsPanel.eager")}
              helper={t("settings.poolsPanel.eagerHelper", { value: position.registration })}
              value={body.eager === true ? "eager" : body.eager === false ? "lazy" : ""}
              options={[
                { value: "", label: t("settings.poolsPanel.positionDefault") },
                { value: "eager", label: t("settings.poolsPanel.eagerOn") },
                { value: "lazy", label: t("settings.poolsPanel.eagerOff") },
              ]}
              onChange={(v) =>
                updateAgent((b) => setField(b, "eager", v === "" ? null : v === "eager"))
              }
            />

            {isRoot ? (
              <div className="space-y-2">
                <Checkbox
                  label={t("settings.poolsPanel.memoryArchive")}
                  checked={archiveOn}
                  onChange={(e) =>
                    updateAgent((b) => {
                      const mem = ensureNested(b, "memory");
                      mem.archive_enabled = e.target.checked;
                      // Core is fed by archive consolidation — never leave the
                      // invalid core-without-archive combination behind.
                      if (!e.target.checked) mem.core_enabled = false;
                      if (!mem.archive_enabled && !mem.core_enabled && !mem.session) {
                        delete b.memory;
                      }
                    })
                  }
                />
                <Checkbox
                  label={t("settings.poolsPanel.memoryCore")}
                  helper={t("settings.poolsPanel.memoryCoreHelper")}
                  checked={coreOn}
                  onChange={(e) =>
                    updateAgent((b) => {
                      const mem = ensureNested(b, "memory");
                      mem.core_enabled = e.target.checked;
                      if (e.target.checked) mem.archive_enabled = true;
                    })
                  }
                />
              </div>
            ) : null}

            <DropdownPanel
              label={t("settings.poolsPanel.promptName")}
              helper={t("settings.poolsPanel.promptHelper")}
              value={asString(body.prompt_name)}
              options={[
                { value: "", label: t("settings.poolsPanel.promptNone") },
                ...prompts.map((p) => ({ value: p, label: p })),
              ]}
              onChange={(v) => updateAgent((b) => setField(b, "prompt_name", v || null))}
            />
          </FormSection>
        </>
      )}
    </div>
  );
}
