import { useState, type FC, type MouseEvent } from "react";

/**
 * Normalized, direction-agnostic view of one attachment for rendering.
 *
 * Both inbound (AttachmentRecord on a user/assistant message) and outbound
 * (attachment_card delta) sources map to this shape before rendering, so a
 * single component handles both. ``kind`` is the renderer's two-way card kind
 * (only images render inline). ``downloadUrl`` already carries the active
 * ``ws`` query param — built by the caller via ``attachmentDownloadUrl``.
 */
export interface AttachmentView {
  id: string;
  kind: "image" | "file";
  name: string;
  size: number;
  mime?: string | null;
  downloadUrl: string;
}

export interface AttachmentRendererProps {
  view: AttachmentView;
}

/** Format a byte count as a human-readable string (1.2 KB, 3.4 MB). */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${units[unit]}`;
}

/**
 * Symmetric attachment renderer (inbound + outbound).
 *
 * - image: inline <img> preview at the download URL; on error/404 a fallback
 *   file-icon card is shown instead of a broken image.
 * - file (and anything else): a file card with name, human size, and a
 *   download link. An anchor exposes no reliable download-error hook, so the
 *   click is intercepted with a HEAD probe: a 4xx (file gone) flips the card
 *   to an explicit, muted "file no longer available" state with no working
 *   link — mirroring the image onError degrade. Other statuses (or a network
 *   failure) let the browser attempt the download as before.
 *
 * The caller sources the active ``ws`` and bakes it into ``downloadUrl``.
 */
export const AttachmentRenderer: FC<AttachmentRendererProps> = ({ view }) => {
  // Image load failure (404 / broken) flips the image to a fallback file card.
  const [imageFailed, setImageFailed] = useState(false);
  // File-card click probe: a 4xx means the underlying file is gone (inbound
  // evicted / outbound deleted) → degrade to an unavailable card.
  const [fileUnavailable, setFileUnavailable] = useState(false);

  if (view.kind === "image" && !imageFailed) {
    return (
      <a
        href={view.downloadUrl}
        target="_blank"
        rel="noreferrer"
        className="mt-1 block max-w-[320px] overflow-hidden rounded-lg border border-card-border-light dark:border-card-border-dark"
        title={view.name}
      >
        <img
          src={view.downloadUrl}
          alt={view.name}
          loading="lazy"
          onError={(): void => setImageFailed(true)}
          className="block max-h-[320px] w-full object-cover"
        />
      </a>
    );
  }

  if (fileUnavailable) {
    // Gone-file degrade: muted card, no download link. Mirrors the image
    // onError visual idiom (degrade in place rather than disappear).
    return (
      <div
        aria-disabled="true"
        className="mt-1 flex max-w-[360px] cursor-not-allowed items-center gap-2.5 rounded-lg border border-card-border-light bg-content-bg-light px-3 py-2 opacity-60 dark:border-card-border-dark dark:bg-content-bg-dark"
        title={view.name}
      >
        <FileIcon />
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="truncate text-[13px] font-medium text-text-secondary-light dark:text-text-secondary-dark">
            {view.name}
          </span>
          <span className="text-[11px] italic text-text-secondary-light dark:text-text-secondary-dark">
            File no longer available
          </span>
        </span>
      </div>
    );
  }

  const onDownloadClick = async (e: MouseEvent<HTMLAnchorElement>): Promise<void> => {
    // Probe before navigating. HEAD avoids downloading the body on success.
    try {
      const probe = await fetch(view.downloadUrl, { method: "HEAD" });
      if (probe.status >= 400 && probe.status < 500) {
        setFileUnavailable(true);
        e.preventDefault();
      }
    } catch {
      // Network/abort error — don't block the browser's own download attempt.
    }
  };

  return (
    <a
      href={view.downloadUrl}
      download={view.name}
      target="_blank"
      rel="noreferrer"
      onClick={onDownloadClick}
      className="mt-1 flex max-w-[360px] items-center gap-2.5 rounded-lg border border-card-border-light bg-content-bg-light px-3 py-2 text-left transition-colors hover:bg-sidebar-hover-light dark:border-card-border-dark dark:bg-content-bg-dark dark:hover:bg-sidebar-hover-dark"
      title={view.name}
    >
      <FileIcon />
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="truncate text-[13px] font-medium text-text-primary-light dark:text-text-primary-dark">
          {view.name}
        </span>
        <span className="text-[11px] text-text-secondary-light dark:text-text-secondary-dark">
          {formatBytes(view.size)}
        </span>
      </span>
    </a>
  );
};

const FileIcon: FC = () => (
  <svg
    width="20"
    height="20"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
    className="shrink-0 text-text-secondary-light dark:text-text-secondary-dark"
  >
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
    <polyline points="14 2 14 8 20 8" />
  </svg>
);
