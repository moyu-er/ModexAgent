// Global skills manager. Loads listSkills() on mount → list of global skills.
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
import { ApiError } from "../../lib/api";
import { useToast } from "../ToastContext";
import { ConfirmDialog } from "./ConfirmDialog";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { IconButton } from "../ui/IconButton";
import { TrashIcon, UploadIcon } from "../ui/icons";

interface Preview {
  name: string;
  files: SkillFile[];
  fileCount: number;
  totalBytes: number;
}

export function GlobalSkillsView() {
  const toast = useToast();
  const [skills, setSkills] = useState<SkillEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string>("");
  const [uploading, setUploading] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [selectedSkill, setSelectedSkill] = useState<string | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [dragOver, setDragOver] = useState(false);
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

  if (loadError) {
    return <p className="text-sm text-error">Failed to load: {loadError}</p>;
  }
  if (!skills) {
    return <p className="text-sm text-mute">Loading…</p>;
  }

  const showPreview = async (files: FileList | File[] | null): Promise<void> => {
    if (!files || files.length === 0) return;
    try {
      const built = await buildUpload(files);
      if (!built) {
        toast.show({
          message: "Could not derive a skill name.",
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
        message: `Could not read files: ${String(e)}`,
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
        message: `Uploaded skill "${preview.name}".`,
        tone: "success",
      });
      setPreview(null);
      if (fileRef.current) fileRef.current.value = "";
      await load();
    } catch (e) {
      toast.show({
        message: `Upload failed: ${e instanceof ApiError ? `${e.status} ${e.detail}` : String(e)}`,
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
      toast.show({ message: `Deleted "${name}".`, tone: "success" });
    } catch (e) {
      toast.show({
        message: `Delete failed: ${e instanceof ApiError ? `${e.status} ${e.detail}` : String(e)}`,
        tone: "warning",
      });
    } finally {
      setPendingDelete(null);
    }
  };

  return (
    <div className="space-y-4">
      <p className="text-xs text-mute">
        Global skills available to every pool's agents.
      </p>

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
        className={`flex cursor-pointer items-center justify-center gap-2 rounded-md border border-dashed px-4 py-6 text-sm transition-colors ${
          dragOver
            ? "border-link bg-hairline-soft text-link"
            : "border-hairline text-mute hover:text-ink"
        }`}
      >
        <UploadIcon className="h-4 w-4" />
        {uploading ? "Uploading…" : "Drop a directory here or click to upload"}
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

      {/* Preview block — shown once files are picked. Confirm triggers
          upload; cancel clears state and resets the file input. */}
      {preview ? (
        <Card className="px-4 py-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 space-y-1">
              <p className="truncate text-sm font-medium text-ink">
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
                Cancel
              </Button>
              <Button
                variant="primary"
                size="sm"
                loading={uploading}
                onClick={() => void confirmUpload()}
              >
                Confirm upload
              </Button>
            </div>
          </div>
        </Card>
      ) : null}

      {skills.length === 0 && !preview ? (
        <p className="rounded-md border border-dashed border-hairline px-3 py-6 text-center text-sm text-mute">
          No global skills uploaded yet.
        </p>
      ) : null}

      <Card>
        <ul className="divide-y divide-hairline">
          {skills.map((s) => {
            const isSelected = selectedSkill === s.name;
            return (
              <li key={s.name}>
                {/* Row */}
                <div
                  className={[
                    "flex cursor-pointer items-center gap-3 border border-transparent px-3 py-2.5 transition-colors",
                    isSelected
                      ? "rounded-t-md bg-canvas-elevated border-hairline"
                      : "rounded-md hover:bg-hairline-soft",
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
                  <span className="truncate text-sm font-medium text-ink">
                    {s.name}
                  </span>
                </div>

                {/* Inline detail pane */}
                {isSelected && (
                  <div className="rounded-b-md border-x border-b border-hairline bg-canvas-elevated px-4 pb-4 pt-2">
                    <div className="flex items-start justify-between gap-3">
                      <h3 className="text-sm font-semibold text-ink">{s.name}</h3>
                      <div className="flex shrink-0 items-center gap-2">
                        <IconButton
                          icon={<TrashIcon />}
                          label={`Delete skill ${s.name}`}
                          variant="danger"
                          size="sm"
                          onClick={(e) => {
                            e.stopPropagation();
                            setPendingDelete(s.name);
                          }}
                        />
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setSelectedSkill(null)}
                        >
                          Close
                        </Button>
                      </div>
                    </div>
                    {s.description ? (
                      <p className="mt-2 text-sm text-body">{s.description}</p>
                    ) : (
                      <p className="mt-2 text-sm text-faint italic">No description.</p>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      </Card>

      {pendingDelete ? (
        <ConfirmDialog
          title={`Delete skill "${pendingDelete}"?`}
          message="The global skill will be removed. Per-agent links that referenced it go dangling."
          confirmLabel="Delete"
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