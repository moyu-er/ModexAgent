import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import {
  AttachmentRenderer,
  type AttachmentView,
} from "./AttachmentRenderer";
import { formatBytes } from "../lib/format";

const imageView: AttachmentView = {
  id: "a1",
  kind: "image",
  name: "pic.png",
  size: 2048,
  mime: "image/png",
  downloadUrl: "/api/sessions/s1/attachments/a1?ws=/w",
};

const fileView: AttachmentView = {
  id: "a2",
  kind: "file",
  name: "notes.txt",
  size: 1024,
  mime: "text/plain",
  downloadUrl: "/api/sessions/s1/attachments/a2?ws=/w",
};

describe("formatBytes", () => {
  it("formats B / KB / MB", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });

  it("handles invalid input", () => {
    expect(formatBytes(-1)).toBe("—");
    expect(formatBytes(Number.NaN)).toBe("—");
  });
});

describe("AttachmentRenderer", () => {
  it("renders inline image preview for kind=image", () => {
    const { container } = render(<AttachmentRenderer view={imageView} />);
    const img = container.querySelector("img") as HTMLImageElement;
    expect(img).toBeTruthy();
    expect(img.getAttribute("src")).toBe(imageView.downloadUrl);
    expect(img.getAttribute("alt")).toBe("pic.png");
  });

  it("renders a file card with name + human size + download link for kind=file", () => {
    render(<AttachmentRenderer view={fileView} />);
    const link = screen.getByText("notes.txt").closest("a") as HTMLAnchorElement;
    expect(link).toBeTruthy();
    expect(link.getAttribute("href")).toBe(fileView.downloadUrl);
    expect(link.getAttribute("download")).toBe("notes.txt");
    expect(screen.getByText("1.0 KB")).toBeTruthy();
  });

  it("falls back to a file card when the image fails to load", () => {
    const { container } = render(<AttachmentRenderer view={imageView} />);
    const img = container.querySelector("img") as HTMLImageElement;
    // Simulate a 404 / broken image: the onError handler flips to fallback.
    fireEvent.error(img);
    // Image is gone; a file-card link with the name + size takes its place.
    expect(container.querySelector("img")).toBeNull();
    const link = screen.getByText("pic.png").closest("a") as HTMLAnchorElement;
    expect(link).toBeTruthy();
    expect(screen.getByText("2.0 KB")).toBeTruthy();
  });

  describe("file-card unavailable fallback (gone file)", () => {
    afterEach(() => {
      vi.restoreAllMocks();
    });

    it("degrades to a muted, link-less card when the click HEAD probe returns 404", async () => {
      const fetchSpy = vi
        .spyOn(globalThis, "fetch")
        .mockResolvedValue(new Response(null, { status: 404 }));

      render(<AttachmentRenderer view={fileView} />);
      const link = screen.getByText("notes.txt").closest("a") as HTMLAnchorElement;
      expect(link).toBeTruthy();

      // Clicking the download anchor fires the HEAD probe.
      fireEvent.click(link);

      // 404 → the anchor is replaced by the unavailable card (no working link).
      await waitFor(() => {
        expect(screen.getByText("File no longer available")).toBeTruthy();
      });
      expect(screen.queryByText("1.0 KB")).toBeNull();
      const maybeName = screen.queryByText("notes.txt");
      expect(maybeName === null || maybeName.closest("a") === null).toBe(true);
      // HEAD probe hit the download URL.
      expect(fetchSpy).toHaveBeenCalledWith(fileView.downloadUrl, { method: "HEAD" });
    });

    it("leaves the working download link in place on a 200 probe", async () => {
      const fetchSpy = vi
        .spyOn(globalThis, "fetch")
        .mockResolvedValue(new Response(null, { status: 200 }));

      render(<AttachmentRenderer view={fileView} />);
      const link = screen.getByText("notes.txt").closest("a") as HTMLAnchorElement;
      fireEvent.click(link);

      // 200 → the unavailable label never appears; the link is intact.
      await waitFor(() => {
        expect(fetchSpy).toHaveBeenCalled();
      });
      expect(screen.queryByText("File no longer available")).toBeNull();
      expect(screen.getByText("1.0 KB")).toBeTruthy();
    });
  });
});
