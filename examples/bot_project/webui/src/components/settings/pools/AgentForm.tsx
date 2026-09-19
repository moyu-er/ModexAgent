// AgentForm.tsx — the assistants panel's per-agent form, v4 (user UI
// corrections 3+4).
//
// ONE friendly face over one draft:
//   - name/purpose (description), working instructions (prompt body via
//     PromptStore, shared-impact notice), automatic naming (session_title)
//   - capability/plugin enablement as plain checkboxes (off writes the
//     real false/veto; on preserves any existing config object untouched)
//   - Skills (immediate assign/unassign) + MCP servers as bounded
//     one-column selection lists (Scope save)
//   - memory & approval EFFECTIVE values with explicit off/reset semantics
//   - collaborators (root only, one level; nested structures survive with
//     an advanced-structure hint)
//
// External agents render the identity-only face (description + provider
// note); they cannot configure native plugins/Skills/MCP. Advanced/custom
// declaration fields never render here but are preserved verbatim on save —
// experts edit them through the raw Scope editor (Settings → Advanced).
//
// Effective memory/approval values come from the bill (disk bill when the
// draft is clean, debounced preview when dirty); edits write declared
// deviations through scopeModel's unified tri-state mutations.

import { useCallback, useMemo, useState } from "react";
import type {
  ScopeAgentBill,
  ScopeModelIssue,
  ScopeOptions,
} from "../../../lib/scopeApi";
import { useT, type MessageKey } from "../../../i18n";
import { Button } from "../../ui/Button";
import { Checkbox } from "../../ui/Checkbox";
import { DropdownPanel } from "../../ui/DropdownPanel";
import { Input } from "../../ui/Input";
import { Textarea } from "../../ui/Textarea";
import { HelperText } from "../../ui/HelperText";
import { SelectionList } from "../../ui/SelectionList";
import { FormSection } from "./FormSection";
import { Badge } from "./chips";
import { AgentSkillSelector } from "../AgentSkillSelector";
import { PromptBodyEditor } from "./PromptBodyEditor";
import {
  addSubagent,
  asString,
  asStringList,
  capabilityMode,
  deleteAgent,
  directCollaborators,
  hasNestedAgents,
  memoryOverride,
  resetApproval,
  resetMemoryLayer,
  sessionTitleOn,
  setApprovalEnabled,
  setCapabilityMode,
  setField,
  setMemoryLayer,
  setSessionTitle,
  toggleInListField,
  type AgentBody,
  type AgentTreeNode,
} from "./scopeModel";

const PLUGIN_LABELS = new Map<string, MessageKey>([
  ["shell", "settings.plugins.shell"],
  ["todo", "settings.plugins.todo"],
  ["experience", "settings.plugins.experience"],
  ["skills", "settings.plugins.skills"],
  ["subagents", "settings.plugins.subagents"],
]);

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
  /**
   * Mutate the WHOLE declaration draft (collaborator add/delete touch
   * structure outside this agent's body). Absent disables structure edits.
   */
  updateModel?: (mut: (draft: import("../../../lib/scopeApi").ScopeModelTree) => void) => void;
  /** The agent's full path (for collaborator structure edits). */
  agentPath?: string[];
  /**
   * Identity gating (PLAN §3.3): false while the agent's identity is an
   * unsaved draft — Skills/MCP/prompt actions that need a persisted
   * identity are hidden with a "save first" notice.
   */
  identityPersisted?: boolean;
  /** Assignment APIs validate the saved declaration, not the live preview. */
  persistedSkillsEnabled: boolean;
  /** Names of pools declared in the draft (collaborator key uniqueness). */
  declaredPools?: string[];
  onChangeInstructions?: (resume: () => void) => void;
  onSelectAgent?: (path: string[]) => void;
}

export function AgentForm({
  pool,
  node,
  options,
  prompts,
  issues,
  bill,
  updateAgent,
  updateModel,
  agentPath,
  identityPersisted = true,
  persistedSkillsEnabled,
  declaredPools = [],
  onChangeInstructions = (resume) => resume(),
  onSelectAgent,
}: Props) {
  const t = useT();
  const body = node.body;
  const isRoot = node.path.length === 1;
  const executionStrategy = asString(body.execution_strategy);
  const isExternal = executionStrategy === "external";

  const capabilitiesMap = useMemo(
    () =>
      body.capabilities !== null && typeof body.capabilities === "object"
        ? (body.capabilities as Record<string, unknown>)
        : null,
    [body.capabilities],
  );
  const capabilityNames = useMemo(() => {
    const declared = Object.keys(capabilitiesMap ?? {});
    const extra = declared.filter((n) => !options.capabilities.includes(n));
    return [...options.capabilities, ...extra];
  }, [capabilitiesMap, options.capabilities]);
  const mcpNames = [
    ...options.mcp_servers,
    ...asStringList(body.mcp).filter((n) => !options.mcp_servers.includes(n)),
  ];

  const memory = ((): Record<string, unknown> | null => {
    const m = body.memory;
    return m !== null && typeof m === "object" && !Array.isArray(m)
      ? (m as Record<string, unknown>)
      : null;
  })();
  const approval = ((): Record<string, unknown> | null => {
    const a = body.approval;
    return a !== null && typeof a === "object" && !Array.isArray(a)
      ? (a as Record<string, unknown>)
      : null;
  })();
  const approvalEnabled = approval?.enabled === true;

  // Effective memory/approval from the bill: display the ACTUAL effective
  // values, not the raw field presence.
  const memoryEffective = bill?.memory ?? null;
  const approvalEffective = bill?.approval ?? null;
  const archiveOverride = memoryOverride(body, "archive_enabled");
  const coreOverride = memoryOverride(body, "core_enabled");
  const approvalOverride = approval?.enabled ?? null;

  const autoNamingOn = sessionTitleOn(body, options.default_hooks);
  const collaborators = useMemo(
    () => (isRoot ? directCollaborators(node) : []),
    [isRoot, node],
  );

  const promptName = asString(body.prompt_name);

  const mcpChecked = useMemo(
    () => new Set(asStringList(body.mcp)),
    [body.mcp],
  );

  /** Capability checkbox: ON preserves any existing config object; OFF
   * writes the real false veto. Uses the real tri-state mutation. */
  const capabilityItems = useMemo(
    () => capabilityNames.map((name) => {
      const key = PLUGIN_LABELS.get(name);
      return { id: name, label: key ? t(key) : name };
    }),
    [capabilityNames, t],
  );
  /**
   * Checkbox state per capability. Precedence (correction 3):
   *   1. explicit declared `false` → off (the veto always wins);
   *   2. declared config object → on (a draft toggle or an expert's config);
   *   3. otherwise follow the bill — checked only when the compiled bill
   *      reports state auto | declared. A capability ABSENT from the bill
   *      (an optional bundle that does not auto-apply, e.g. experience on
   *      a declaration without it) renders OFF; enabling it writes the
   *      real `on` deviation.
   */
  const capabilityChecked = useMemo(() => {
    const set = new Set<string>();
    for (const name of capabilityNames) {
      const mode = capabilityMode(body, name);
      if (mode === "off") continue;
      if (mode === "on") {
        set.add(name);
        continue;
      }
      const billState = bill?.capabilities.find(
        (c) => c.capability === name,
      )?.state;
      if (billState === "auto" || billState === "declared") set.add(name);
    }
    return set;
  }, [capabilityNames, bill, body]);

  return (
    <div className="space-y-4" data-testid="pools-agent-form">
      {!isRoot && onSelectAgent && <Button variant="ghost" size="sm" onClick={() => onSelectAgent(node.path.slice(0, 1))}>{t("settings.poolsPanel.backToAssistant")}</Button>}
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
          {issues.map((issue) => (
            <li key={`${issue.rule}-${issue.node}-${issue.message}`} className="text-xs text-danger">
              <span className="font-mono font-semibold">{issue.rule}</span> {issue.message}
            </li>
          ))}
        </ul>
      ) : null}

      {!identityPersisted ? (
        <p
          data-testid="identity-save-first"
          className="rounded-sm border border-warning bg-canvas-elevated px-3 py-2 text-xs text-warning"
        >
          {t("settings.poolsPanel.identitySaveFirst")}
        </p>
      ) : null}

      {/* ── Overview ──────────────────────────────────────────────────── */}
      <FormSection title={t("settings.poolsPanel.sectionFriendly")}>
        <Textarea
          label={t("settings.poolsPanel.description")}
          helper={t("settings.poolsPanel.descriptionHelper")}
          mono={false}
          value={asString(body.description)}
          onChange={(e) =>
            updateAgent((b) => setField(b, "description", e.target.value || null))
          }
        />

        {/* Working instructions: direct prompt-body edit through the
            original PromptStore owner (immediate save; shared-impact
            notice). Custom prompt/provider combinations fall back to the
            prompt_name selector (not forced into prompt_name). */}
        {!isExternal ? (
          <>
            <DropdownPanel
              label={t("settings.poolsPanel.promptName")}
              helper={t("settings.poolsPanel.promptHelper")}
              value={promptName}
              options={[
                { value: "", label: t("settings.poolsPanel.promptNone") },
                ...prompts.map((p) => ({ value: p, label: p })),
              ]}
              onChange={(v) => onChangeInstructions(() => updateAgent((b) => setField(b, "prompt_name", v || null)))}
            />
            {promptName ? (
              <PromptBodyEditor promptName={promptName} identityPersisted={identityPersisted} />
            ) : (
              <HelperText>{t("settings.poolsPanel.promptBodyMissing")}</HelperText>
            )}
          </>
        ) : (
          <HelperText>{t("settings.poolsPanel.providerOwns", { cli: asString(body.provider_kind) || "external" })}</HelperText>
        )}

        {isRoot ? (
          <Checkbox
            label={t("settings.poolsPanel.autoNaming")}
            helper={t("settings.poolsPanel.autoNamingHelper")}
            checked={autoNamingOn}
            onChange={(e) =>
              updateAgent((b) =>
                setSessionTitle(b, e.target.checked, options.default_hooks),
              )
            }
          />
        ) : null}
      </FormSection>

      {/* Collaborators — root only, one level, nested preserved */}
      {isRoot && !isExternal ? (
        <FormSection title={t("settings.poolsPanel.sectionCollaborators")}>
          <HelperText>{t("settings.poolsPanel.collaboratorsHelper")}</HelperText>
          <div className="space-y-2" data-testid="collaborator-list">
            {collaborators.length === 0 ? (
              <p className="text-sm text-mute">{t("settings.poolsPanel.noCollaborators")}</p>
            ) : (
              collaborators.map((child) => (
                <div
                  key={child.name}
                  className="flex items-center gap-2 rounded-md border border-hairline px-3 py-2"
                >
                  <button type="button" className="min-w-0 flex-1 truncate rounded text-left font-mono text-sm text-ink hover:text-brand" aria-label={t("settings.poolsPanel.editCollaborator", { name: child.name })} onClick={() => onSelectAgent?.(child.path)} disabled={!onSelectAgent}>
                    {child.name}
                  </button>
                  {hasNestedAgents(child) ? (
                    <span title={t("settings.poolsPanel.collaboratorNestedTitle")}>
                      <Badge>{t("settings.poolsPanel.collaboratorNested")}</Badge>
                    </span>
                  ) : null}
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() =>
                      updateModel?.((draft) =>
                        agentPath ? deleteAgent(draft, pool, [...agentPath, child.name]) : undefined,
                      )
                    }
                    disabled={!updateModel || !agentPath}
                    aria-label={t("settings.pools.removeSubagent", { name: child.name })}
                  >
                    {t("common.remove")}
                  </Button>
                </div>
              ))
            )}
          </div>
          {updateModel && agentPath ? (
            <AddCollaboratorButton
              parentPath={agentPath}
              existingNames={[
                ...collaborators.map((c) => c.name),
                ...declaredPools,
              ]}
              onAdd={(name) =>
                updateModel((draft) => addSubagent(draft, pool, agentPath, name))
              }
            />
          ) : null}
        </FormSection>
      ) : null}
      {isRoot && isExternal ? (
        <HelperText>{t("settings.poolsPanel.externalNoCollaborators")}</HelperText>
      ) : null}

      {/* Capabilities & plugins — plain enable checkboxes (Scope save) */}
      {!isExternal && capabilityNames.length > 0 ? (
        <FormSection title={t("settings.poolsPanel.sectionCapabilities")}>
          <HelperText>{t("settings.poolsPanel.capabilitiesHelper")}</HelperText>
          <SelectionList
            items={capabilityItems}
            checked={capabilityChecked}
            onToggle={(name, next) =>
              updateAgent((b) =>
                setCapabilityMode(b, name, next ? "on" : "off"),
              )
            }
            ariaLabel={t("settings.poolsPanel.sectionCapabilities")}
            searchLabel={
              capabilityItems.length > 5
                ? t("settings.poolsPanel.filterPlaceholder")
                : undefined
            }
          />
        </FormSection>
      ) : null}

      {/* Skills / MCP — instant-effect selectors, identity gated */}
      {!isExternal && identityPersisted ? (
        <FormSection title={t("settings.poolsPanel.sectionExtensions")}>
          {persistedSkillsEnabled && capabilityMode(body, "skills") !== "off" ? (
            <AgentSkillSelector pool={pool} agent={node.name} />
          ) : (
            <HelperText>{t("settings.poolsPanel.skillsDisabled")}</HelperText>
          )}
          <div>
            <span className="mb-1 block text-base text-ink">
              {t("settings.poolsPanel.mcp")}
            </span>
            {mcpNames.length === 0 ? (
              <HelperText>{t("settings.poolsPanel.mcpEmpty")}</HelperText>
            ) : (
              <SelectionList
                items={mcpNames.map((name) => ({ id: name, label: name }))}
                checked={mcpChecked}
                onToggle={(name, next) =>
                  updateAgent((b) => toggleInListField(b, "mcp", name, next))
                }
                ariaLabel={t("settings.poolsPanel.mcp")}
                searchLabel={
                  mcpNames.length > 5
                    ? t("settings.poolsPanel.filterPlaceholder")
                    : undefined
                }
              />
            )}
          </div>
        </FormSection>
      ) : null}

      {/* Memory & confirmation — effective values + explicit off/reset */}
      {!isExternal && isRoot ? (
        <FormSection title={t("settings.poolsPanel.memorySection")}>
          <div className="space-y-3">
            <MemoryToggle
              label={t("settings.poolsPanel.memoryArchive")}
              effective={memoryEffective?.archive_enabled ?? (memory?.archive_enabled === true)}
              override={archiveOverride}
              onSet={(on) => updateAgent((b) => setMemoryLayer(b, "archive_enabled", on))}
              onReset={() => updateAgent((b) => resetMemoryLayer(b, "archive_enabled"))}
            />
            <MemoryToggle
              label={t("settings.poolsPanel.memoryCore")}
              helper={t("settings.poolsPanel.memoryCoreHelper")}
              effective={memoryEffective?.core_enabled ?? (memory?.core_enabled === true)}
              override={coreOverride}
              onSet={(on) => updateAgent((b) => setMemoryLayer(b, "core_enabled", on))}
              onReset={() => updateAgent((b) => resetMemoryLayer(b, "core_enabled"))}
            />
            <div className="rounded-md border border-hairline p-3">
              <Checkbox
                label={t("settings.poolsPanel.approvalRow")}
                checked={approvalEffective?.enabled ?? approvalEnabled}
                disabled={approvalEffective ? !approvalEffective.eligible : false}
                onChange={(e) =>
                  updateAgent((b) => setApprovalEnabled(b, e.target.checked))
                }
              />
              {approvalEffective && !approvalEffective.eligible ? (
                <HelperText>{t("settings.poolsPanel.approvalIneligible")}</HelperText>
              ) : null}
              {approvalOverride !== null ? (
                <div className="mt-2 flex items-center gap-2">
                  <span className="text-xs text-mute">
                    {approvalOverride
                      ? t("settings.poolsPanel.memoryOverrideOn")
                      : t("settings.poolsPanel.memoryOverrideOff")}
                  </span>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => updateAgent((b) => resetApproval(b))}
                  >
                    {t("settings.poolsPanel.resetToDefault")}
                  </Button>
                </div>
              ) : null}
            </div>
          </div>
        </FormSection>
      ) : null}
    </div>
  );
}

// ── Memory toggle with effective value + explicit off/reset ─────────────────

function MemoryToggle({
  label,
  helper,
  effective,
  override,
  onSet,
  onReset,
}: {
  label: string;
  helper?: string;
  effective: boolean;
  override: boolean | null;
  onSet: (on: boolean) => void;
  onReset: () => void;
}) {
  const t = useT();
  return (
    <div className="rounded-md border border-hairline p-3">
      <Checkbox
        label={label}
        helper={helper}
        checked={effective}
        onChange={(e) => onSet(e.target.checked)}
      />
      <div className="mt-1.5 flex items-center gap-2">
        <span className="text-xs text-mute">
          {override === null
            ? t("settings.poolsPanel.memoryFollowsDefault")
            : override
              ? t("settings.poolsPanel.memoryOverrideOn")
              : t("settings.poolsPanel.memoryOverrideOff")}
        </span>
        {override !== null ? (
          <Button variant="secondary" size="sm" onClick={onReset}>
            {t("settings.poolsPanel.resetToDefault")}
          </Button>
        ) : null}
      </div>
    </div>
  );
}

// ── Add-collaborator inline input ───────────────────────────────────────────

const KEY_RE = /^[a-z][a-z0-9_-]*$/;

function AddCollaboratorButton({
  parentPath,
  existingNames,
  onAdd,
}: {
  parentPath: string[];
  existingNames: string[];
  onAdd: (name: string) => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState<string>("");

  const submit = useCallback((): void => {
    const trimmed = name.trim();
    if (!KEY_RE.test(trimmed)) {
      setError(t("settings.poolsPanel.newAgentInvalidKey"));
      return;
    }
    if (existingNames.includes(trimmed)) {
      setError(t("settings.poolsPanel.newAgentKeyTaken", { name: trimmed }));
      return;
    }
    onAdd(trimmed);
    setName("");
    setError("");
    setOpen(false);
  }, [name, existingNames, onAdd, t]);

  if (!open) {
    return (
      <Button variant="secondary" size="sm" onClick={() => setOpen(true)}>
        {t("settings.poolsPanel.addCollaborator")}
      </Button>
    );
  }
  void parentPath;
  return (
    <div className="space-y-1.5" data-testid="add-collaborator">
      <Input
        label={t("settings.poolsPanel.newAgentHeading")}
        value={name}
        placeholder={t("settings.poolsPanel.newAgentKeyPlaceholder")}
        autoFocus
        onChange={(e) => {
          setName(e.target.value);
          setError("");
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            submit();
          } else if (e.key === "Escape") {
            setOpen(false);
            setName("");
            setError("");
          }
        }}
      />
      {error ? <p className="text-xs text-danger">{error}</p> : null}
      <div className="flex gap-2">
        <Button variant="primary" size="sm" onClick={submit}>
          {t("common.add")}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => {
            setOpen(false);
            setName("");
            setError("");
          }}
        >
          {t("common.cancel")}
        </Button>
      </div>
    </div>
  );
}
