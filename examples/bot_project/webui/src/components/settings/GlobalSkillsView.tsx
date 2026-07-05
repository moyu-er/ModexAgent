// Global skills manager. Loads listSkills() on mount → list of global skills.
// Upload via <input type="file" webkitdirectory>: collect files, read each via
// file.arrayBuffer() → base64, derive skill name from the top dir of
// webkitRelativePath, POST via uploadSkill(name, files). Delete via deleteSkill.
//
// webkitdirectory is a non-standard attribute. We attach it via a spread escape
// ({...{webkitdirectory: ""}}) so the JSX stays type-clean without a global
// module declaration. happy-dom does not fully simulate directory input, so the
// upload is tested by calling the handler with a synthetic FileList.

import { useEffect, useRef, useState } from "react";
import type { SkillEntry } from "../../types/pool";
import { listSkills, uploadSkill, deleteSkill } from "../../lib/skillsApi";
import type { SkillFile } from "../../lib/skillsApi";
import { ApiError } from "../../lib/api";
import { useToast } from "../ToastContext";
import { ConfirmDialog } from "./ConfirmDialog";

export function GlobalSkillsView() {
  const toast = useToast();
  const [skills, setSkills] = useState<SkillEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string>("");
  const [uploading, setUploading] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
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
    return <p className="text-sm text-text-secondary">Loading…</p>;
  }

  const onUpload = async (
    files: FileList | null,
  ): Promise<void> => {
    if (!files || files.length === 0) return;
    setUploading(true);
    try {
      const built = await buildUpload(files);
      if (!built) {
        toast.show({ message: "Could not derive a skill name.", tone: "warning" });
        return;
      }
      await uploadSkill(built.name, built.files);
      toast.show({ message: `Uploaded skill "${built.name}".`, tone: "success" });
      await load();
      // Reset the input so selecting the same dir again re-fires onChange.
      if (fileRef.current) fileRef.current.value = "";
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
      <div className="flex items-center justify-between">
        <p className="text-xs text-text-secondary">
          Global skills available to every pool's agents.
        </p>
        <label className="cursor-pointer rounded-md border border-input-border px-3 py-1.5 text-sm text-text-primary hover:bg-sidebar-hover">
          {uploading ? "Uploading…" : "+ Upload directory"}
          <input
            ref={fileRef}
            type="file"
            // webkitdirectory is non-standard; spread-escape avoids a global
            // module declaration while keeping the JSX type-clean.
            {...{ webkitdirectory: "" }}
            multiple
            className="hidden"
            onChange={(ev) => {
              void onUpload(ev.currentTarget.files);
            }}
            disabled={uploading}
          />
        </label>
      </div>

      {skills.length === 0 && (
        <p className="rounded-md border border-dashed border-input-border px-3 py-6 text-center text-sm text-text-secondary">
          No global skills uploaded yet.
        </p>
      )}

      <ul className="divide-y divide-divider rounded-lg border border-card-border bg-content-bg">
        {skills.map((s) => (
          <li
            key={s.name}
            className="flex items-center gap-3 px-3 py-2.5"
          >
            <span className="truncate text-sm font-medium text-text-primary">
              {s.name}
            </span>
            <span className="rounded-full border border-card-border px-2 py-0.5 text-[11px] text-text-secondary">
              {s.source}
            </span>
            <button
              type="button"
              aria-label={`Delete skill ${s.name}`}
              className="ml-auto text-text-secondary hover:text-error"
              onClick={() => setPendingDelete(s.name)}
            >
              <TrashIcon />
            </button>
          </li>
        ))}
      </ul>

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

function TrashIcon() {
  return (
    <svg className="h-4 w-4" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M3 4h10M6.5 4V2.5h3V4M5 4l.5 8.5a1 1 0 0 0 1 .9h3a1 1 0 0 0 1-.9L11 4M6.5 7v4M9.5 7v4"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
