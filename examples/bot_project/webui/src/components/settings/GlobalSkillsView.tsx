// Skills settings view. Loads the global library and scope topology on mount.
//
// Upload flow: the user drops a directory onto the drop zone (or clicks it to
// open the native directory picker). Either path populates a `preview` block
// showing derived skill name, file count, and total bytes. Confirm uploads,
// cancel clears the preview.
//
// webkitdirectory is a non-standard attribute; we attach it via a spread
// escape ({...{webkitdirectory: ""}}) so the JSX stays type-clean without a
// global module declaration. happy-dom does not fully simulate the directory
// input, so the upload is tested by calling the handler with a synthetic
// FileList or by firing change on the hidden input.

import { useEffect, useRef, useState } from "react";
import type { SkillEntry } from "../../types/pool";
import { listSkills, uploadSkill, deleteSkill } from "../../lib/skillsApi";
import type { SkillFile } from "../../lib/skillsApi";
import { getScopeTopology } from "../../lib/scopeApi";
import type { ScopePoolTopology, ScopeTopology } from "../../lib/scopeApi";
import { ApiError } from "../../lib/api";
import { useToast } from "../ToastContext";
import { AgentSkillSelector } from "./AgentSkillSelector";
import { ConfirmDialog } from "./ConfirmDialog";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { DropdownPanel } from "../ui/DropdownPanel";
import { IconButton } from "../ui/IconButton";
import { SectionLabel } from "../ui/SectionLabel";
import { Trash2 } from "lucide-react";
import { UploadIcon } from "../ui/icons";
import { CATEGORY } from "./categoryMeta";
import { useT } from "../../i18n";

interface Preview {
  name: string;
  files: SkillFile[];
  fileCount: number;
  totalBytes: number;
}

export function GlobalSkillsView() {
  const toast = useToast();
  const t = useT();
  const [skills, setSkills] = useState<SkillEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string>("");
  const [uploading, setUploading] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [selectedSkill, setSelectedSkill] = useState<string | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [query, setQuery] = useState<string>("");
  const [topology, setTopology] = useState<ScopeTopology | null>(null);
  const [topologyError, setTopologyError] = useState<string>("");
  const [selectedPool, setSelectedPool] = useState<string>("");
  const [selectedAgent, setSelectedAgent] = useState<string>("");
  const fileRef = useRef<HTMLInputElement | null>(null);

  const load = async (): Promise<void> => {
    setLoadError("");
    try {
      setSkills(await listSkills());
    } catch (e) {
      setLoadError(String(e));
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const eligibleAgents = (pool: ScopePoolTopology | undefined) =>
    pool?.agents.filter((agent) => agent.skills_eligible) ?? [];

  const rootAgent = (pool: ScopePoolTopology | undefined): string => {
    const agents = eligibleAgents(pool);
    return agents.find((agent) => agent.root)?.name ?? agents[0]?.name ?? "";
  };

  const loadTopology = async (): Promise<void> => {
    setTopology(null);
    setTopologyError("");
    try {
      const next = await getScopeTopology();
      const firstPool = next.pools.find(
        (pool) => eligibleAgents(pool).length > 0,
      );
      setTopology(next);
      setSelectedPool(firstPool?.name ?? "");
      setSelectedAgent(rootAgent(firstPool));
    } catch (e) {
      setTopologyError(String(e));
    }
  };

  useEffect(() => {
    void loadTopology();
  }, []);

  if (loadError) {
    return <p className="text-base text-error">{t("common.failedToLoad", { error: loadError })}</p>;
  }
  if (!skills) {
    return <p className="text-base text-mute">{t("common.loading")}</p>;
  }

  const sortedSkills = skills
    .filter((s) => s.name.toLowerCase().includes(query.toLowerCase()))
    .sort((a, b) => a.name.localeCompare(b.name));

  const showPreview = async (files: FileList | File[] | null): Promise<void> => {
    if (!files || files.length === 0) return;
    try {
      const built = await buildUpload(files);
      if (!built) {
        toast.show({
          message: t("settings.skills.noName"),
          tone: "warning",
        });
        return;
      }
      const totalBytes = built.files.reduce((sum, f) => {
        // base64 length → bytes: 4 chars → 3 bytes, rounded up.
        const padding = (f.content.match(/=+$/)?.[0] ?? "").length;
        return sum + Math.floor((f.content.length * 3) / 4) - padding;
      }, 0);
      setPreview({
        name: built.name,
        files: built.files,
        fileCount: built.files.length,
        totalBytes,
      });
    } catch (e) {
      toast.show({
        message: t("settings.skills.readFailed", { detail: String(e) }),
        tone: "warning",
      });
    }
  };

  const cancelPreview = (): void => {
    setPreview(null);
    if (fileRef.current) fileRef.current.value = "";
  };

  const confirmUpload = async (): Promise<void> => {
    if (!preview) return;
    setUploading(true);
    try {
      await uploadSkill(preview.name, preview.files);
      toast.show({
        message: t("settings.skills.uploaded", { name: preview.name }),
        tone: "success",
      });
      setPreview(null);
      if (fileRef.current) fileRef.current.value = "";
      await load();
    } catch (e) {
      toast.show({
        message: t("settings.skills.uploadFailed", { detail: e instanceof ApiError ? `${e.status} ${e.detail}` : String(e) }),
        tone: "warning",
      });
    } finally {
      setUploading(false);
    }
  };

  const onDeleteConfirmed = async (name: string): Promise<void> => {
    try {
      await deleteSkill(name);
      setSkills((prev) => (prev ?? []).filter((s) => s.name !== name));
      if (selectedSkill === name) {
        setSelectedSkill(null);
      }
      toast.show({ message: t("settings.skills.deleted", { name }), tone: "success" });
    } catch (e) {
      toast.show({
        message: t("settings.skills.deleteFailed", { detail: e instanceof ApiError ? `${e.status} ${e.detail}` : String(e) }),
        tone: "warning",
      });
    } finally {
      setPendingDelete(null);
    }
  };

  const meta = CATEGORY.skills;
  const PageHeadIcon = meta.icon;

  return (
    <div className="space-y-4">
      <div className="page-head">
        <span
          className="page-head-icon"
          style={{ ["--cat" as string]: meta.catVar }}
        >
          <PageHeadIcon size={18} />
        </span>
        <div>
          <div className="page-title">{meta.titleTerm ?? t(meta.titleKey!)}</div>
          <div className="page-sub">{t(meta.subKey)}</div>
        </div>
      </div>

      <section
        aria-label={t("settings.skills.agentAssignments")}
        className="space-y-3"
      >
        <SectionLabel>{t("settings.skills.agentAssignments")}</SectionLabel>
        {topologyError ? (
          <Card className="space-y-3">
            <p role="alert" className="text-base text-error">
              {t("common.failedToLoad", { error: topologyError })}
            </p>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void loadTopology()}
            >
              {t("common.retry")}
            </Button>
          </Card>
        ) : topology === null ? (
          <p className="text-base text-mute">{t("common.loading")}</p>
        ) : topology.pools.length === 0 ? (
          <p className="rounded-md border border-dashed border-hairline px-3 py-6 text-center text-base text-mute">
            {t("settings.skills.noAgents")}
          </p>
        ) : (
          <div className="space-y-3">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <DropdownPanel
                label={t("settings.skills.pool")}
                options={topology.pools
                  .filter((pool) => eligibleAgents(pool).length > 0)
                  .map((pool) => ({
                    value: pool.name,
                    label: pool.name,
                  }))}
                value={selectedPool}
                onChange={(poolName) => {
                  const pool = topology.pools.find(
                    (candidate) => candidate.name === poolName,
                  );
                  setSelectedPool(poolName);
                  setSelectedAgent(rootAgent(pool));
                }}
              />
              <DropdownPanel
                label={t("settings.skills.agent")}
                options={eligibleAgents(
                  topology.pools.find(
                    (pool) => pool.name === selectedPool,
                  ),
                ).map((agent) => ({
                  value: agent.name,
                  label: agent.name,
                }))}
                value={selectedAgent}
                disabled={!selectedAgent}
                onChange={setSelectedAgent}
              />
            </div>
            {selectedPool && selectedAgent ? (
              <AgentSkillSelector
                key={`${selectedPool}:${selectedAgent}`}
                pool={selectedPool}
                agent={selectedAgent}
                globalSkills={skills}
              />
            ) : (
              <p className="rounded-md border border-dashed border-hairline px-3 py-6 text-center text-base text-mute">
                {t("settings.skills.noAgents")}
              </p>
            )}
          </div>
        )}
      </section>

      <section
        aria-label={t("settings.skills.globalLibrary")}
        className="space-y-4"
      >
        <div>
          <SectionLabel>{t("settings.skills.globalLibrary")}</SectionLabel>
          <p className="text-xs text-mute">
            {t("settings.skills.availableToAll")}
          </p>
        </div>

      {/* Drop zone — wraps the hidden directory picker input so clicking
          the zone opens the native picker, and dropping files populates the
          same upload flow. */}
      <label
        htmlFor="skill-upload"
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragEnter={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          // happy-dom / browsers vary on e.dataTransfer.items vs .files;
          // prefer items when present (lets us filter directories later),
          // fall back to files for synthetic test events.
          void showPreview(e.dataTransfer?.files ?? null);
        }}
        className={`flex cursor-pointer items-center justify-center gap-2 rounded-md border border-dashed px-4 py-6 text-base transition-colors ${
          dragOver
            ? "border-link bg-hairline-soft text-link"
            : "border-hairline text-mute hover:text-ink"
        }`}
      >
        <UploadIcon className="h-4 w-4" />
        {uploading ? t("settings.skills.uploading") : t("settings.skills.dropOrClick")}
        <input
          ref={fileRef}
          id="skill-upload"
          type="file"
          {...{ webkitdirectory: "" }}
          multiple
          className="hidden"
          onChange={(ev) => {
            void showPreview(ev.currentTarget.files);
          }}
          disabled={uploading}
        />
      </label>

      {skills.length > 0 && (
        <div className="flex items-center gap-2 rounded-md border border-hairline bg-canvas-elevated px-3 py-2">
          <svg
            aria-hidden="true"
            className="h-4 w-4 shrink-0 text-mute"
            fill="none"
            viewBox="0 0 16 16"
            xmlns="http://www.w3.org/2000/svg"
          >
            <path
              d="M7 12A5 5 0 107 2a5 5 0 000 10zM11 11l4 4"
              stroke="currentColor"
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth="1.5"
            />
          </svg>
          <input
            type="text"
            aria-label={t("settings.skills.searchSkills")}
            placeholder={t("settings.skills.searchPlaceholder")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="w-full bg-transparent text-base text-ink placeholder:text-faint focus:outline-none"
          />
        </div>
      )}

      {/* Preview block — shown once files are picked. Confirm triggers
          upload; cancel clears state and resets the file input. */}
      {preview ? (
        <Card className="px-4 py-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 space-y-1">
              <p className="truncate text-base font-medium text-ink">
                {preview.name}
              </p>
              <p className="text-xs text-mute">
                {preview.fileCount} file{preview.fileCount === 1 ? "" : "s"}
                {" · "}
                {formatBytes(preview.totalBytes)}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Button
                variant="secondary"
                size="sm"
                onClick={cancelPreview}
                disabled={uploading}
              >
                {t("settings.skills.cancel")}
              </Button>
              <Button
                variant="primary"
                size="sm"
                loading={uploading}
                onClick={() => void confirmUpload()}
              >
                {t("settings.skills.confirmUpload")}
              </Button>
            </div>
          </div>
        </Card>
      ) : null}

      {skills.length === 0 && !preview ? (
        <p className="rounded-md border border-dashed border-hairline px-3 py-6 text-center text-base text-mute">
          {t("settings.skills.noSkills")}
        </p>
      ) : null}

      {sortedSkills.length > 0 && (
        <Card>
          <div className="space-y-1 p-1">
            {sortedSkills.map((s) => {
              const isSelected = selectedSkill === s.name;
              return (
                <div key={s.name}>
                  <div
                    className={[
                      "flex cursor-pointer items-center gap-3 rounded-md border px-3 py-2.5 transition-colors",
                      isSelected
                        ? "border-hairline bg-hairline-soft"
                        : "border-transparent hover:bg-hairline-soft",
                    ].join(" ")}
                    role="button"
                    tabIndex={0}
                    onClick={() => setSelectedSkill(isSelected ? null : s.name)}
                    onKeyDown={(e): void => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setSelectedSkill(isSelected ? null : s.name);
                      }
                    }}
                  >
                    <span className="flex-1 truncate font-mono text-base font-medium text-ink">
                      {s.name}
                    </span>
                    {s.origin && (
                      <span
                        className="rounded-full border border-hairline px-1.5 py-0.5 font-mono text-xs uppercase tracking-eyebrow text-mute"
                        title={
                          s.origin === "repo"
                            ? t("settings.skills.repoOriginTitle")
                            : t("settings.skills.userOriginTitle")
                        }
                      >
                        {s.origin === "repo" ? t("settings.skills.local") : t("settings.skills.global")}
                      </span>
                    )}
                    {s.description && (
                      <span className="shrink-0 text-xs text-mute">
                        {s.description.length > 60
                          ? s.description.slice(0, 60) + "\u2026"
                          : s.description}
                      </span>
                    )}
                  </div>

                  {isSelected && (
                    <div className="rounded-b-md border-x border-b border-hairline bg-canvas-elevated px-4 pb-4 pt-2">
                      <div className="flex items-start justify-between gap-3">
                        <h3 className="text-base font-semibold text-ink">{s.name}</h3>
                        <div className="flex shrink-0 items-center gap-2">
                          {s.origin === "repo" && (
                            <IconButton
                              icon={<Trash2 size={16} />}
                              label={t("settings.skills.deleteSkill", { name: s.name })}
                              variant="danger"
                              size="sm"
                              onClick={(e) => {
                                e.stopPropagation();
                                setPendingDelete(s.name);
                              }}
                            />
                          )}
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setSelectedSkill(null)}
                          >
                            {t("settings.skills.close")}
                          </Button>
                        </div>
                      </div>
                      {s.description ? (
                        <p className="mt-2 text-base text-body">{s.description}</p>
                      ) : (
                        <p className="mt-2 text-base text-faint italic">
                          {t("settings.skills.noDescription")}
                        </p>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </Card>
      )}

      {skills !== null && skills.length > 0 && sortedSkills.length === 0 && (
        <p className="rounded-md border border-dashed border-hairline px-3 py-6 text-center text-base text-mute">
          {t("settings.skills.noMatch", { query })}
        </p>
      )}
      </section>

      {pendingDelete ? (
        <ConfirmDialog
          title={t("settings.skills.deleteTitle", { name: pendingDelete })}
          message={t("settings.skills.deleteMessage")}
          confirmLabel={t("settings.skills.delete")}
          tone="danger"
          onConfirm={() => void onDeleteConfirmed(pendingDelete)}
          onCancel={() => setPendingDelete(null)}
        />
      ) : null}
    </div>
  );
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Build the upload payload from a directory FileList. The skill name is the
 * top-level directory of the first file's webkitRelativePath. Each file is
 * rebased relative to that top dir and base64-encoded.
 *
 * Exported for tests so the payload-construction logic can be exercised with a
 * synthetic FileList (happy-dom does not simulate the webkitdirectory picker).
 */
export async function buildUpload(
  files: FileList | File[],
): Promise<{ name: string; files: SkillFile[] } | null> {
  const arr = Array.from(files);
  if (arr.length === 0) return null;
  const first = arr[0]!;
  // webkitRelativePath is populated by the directory picker; fall back to name
  // (single-file select) so the helper is robust outside the picker context.
  const rel =
    (first as File & { webkitRelativePath?: string }).webkitRelativePath ||
    first.name;
  const segments = rel.split("/");
  const name = segments.length > 1 ? segments[0]! : stripSkillExt(first.name);
  if (!name) return null;

  const out: SkillFile[] = [];
  for (const f of arr) {
    const fRel =
      (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name;
    // Rebase relative to the top dir.
    const relpath =
      segments.length > 1 && fRel.startsWith(`${name}/`)
        ? fRel.slice(name.length + 1)
        : fRel;
    const buf = await f.arrayBuffer();
    out.push({ relpath, content: bytesToBase64(new Uint8Array(buf)) });
  }
  return { name, files: out };
}

function stripSkillExt(filename: string): string {
  return filename.replace(/\.md$/i, "");
}

/** Minimal byte→base64 without node 'Buffer' (works in browser + happy-dom). */
function bytesToBase64(bytes: Uint8Array): string {
  let bin = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}
