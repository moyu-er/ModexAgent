import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { WorkspaceBrowser } from "./WorkspaceBrowser";
import * as api from "../lib/api";

vi.mock("../lib/api", () => ({
  pickWorkspace: vi.fn(),
  changeWorkspace: vi.fn(),
  ApiError: class ApiError extends Error {
    readonly status: number;
    readonly statusText: string;
    readonly detail: string;
    constructor(status: number, statusText: string, detail: string) {
      super(`API ${status} ${statusText}${detail ? `: ${detail}` : ""}`);
      this.name = "ApiError";
      this.status = status;
      this.statusText = statusText;
      this.detail = detail;
    }
  },
}));

const mockedPickWorkspace = vi.mocked(api.pickWorkspace);
const mockedChangeWorkspace = vi.mocked(api.changeWorkspace);

const noop = (): void => {};

describe("WorkspaceBrowser", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  function renderBrowser(props: Partial<React.ComponentProps<typeof WorkspaceBrowser>> = {}) {
    const utils = render(
      <WorkspaceBrowser
        open={true}
        onClose={noop}
        onChanged={noop}
        onGoHome={noop}
        recentWorkspaces={[]}
        {...props}
      />,
    );
    // WorkspaceBrowser renders via createPortal(document.body), so text
    // assertions must query document.body, not utils.container.
    return { ...utils, body: document.body };
  }

  describe("open folder picker", () => {
    it("calls pickWorkspace and switches on success (single request)", async () => {
      mockedPickWorkspace.mockResolvedValue({
        path: "/selected/path",
        success: true,
        cwd: "/selected/path",
      });
      const onChanged = vi.fn();
      const onClose = vi.fn();
      const { getByText } = renderBrowser({ onChanged, onClose });

      fireEvent.click(getByText("Open Folder"));

      await waitFor(() => {
        expect(mockedPickWorkspace).toHaveBeenCalledTimes(1);
        expect(mockedChangeWorkspace).not.toHaveBeenCalled();
        expect(onChanged).toHaveBeenCalledWith("/selected/path");
        expect(onClose).toHaveBeenCalled();
      });
    });

    it("does nothing when user cancels the picker (path is null)", async () => {
      mockedPickWorkspace.mockResolvedValue({ path: null, success: false });
      const onChanged = vi.fn();
      const onClose = vi.fn();
      const { getByText } = renderBrowser({ onChanged, onClose });

      fireEvent.click(getByText("Open Folder"));

      await waitFor(() => {
        expect(mockedPickWorkspace).toHaveBeenCalled();
      });
      expect(onChanged).not.toHaveBeenCalled();
      expect(onClose).not.toHaveBeenCalled();
    });

    it("shows error message when picker returns 503 (no display)", async () => {
      mockedPickWorkspace.mockRejectedValue(new api.ApiError(503, "Service Unavailable", ""));
      const { getByText, body } = renderBrowser();

      fireEvent.click(getByText("Open Folder"));

      await waitFor(() => {
        expect(body.textContent).toContain("not available");
      });
    });

    it("shows network error on non-503 API errors", async () => {
      mockedPickWorkspace.mockRejectedValue(new api.ApiError(500, "Internal Server Error", ""));
      const { getByText, body } = renderBrowser();

      fireEvent.click(getByText("Open Folder"));

      await waitFor(() => {
        expect(body.textContent).toContain("Network error");
      });
    });

    it("shows error when pick returns success=false with notice", async () => {
      mockedPickWorkspace.mockResolvedValue({
        path: "/bad/path",
        success: false,
        notice: "Permission denied",
      });
      const { getByText, body } = renderBrowser();

      fireEvent.click(getByText("Open Folder"));

      await waitFor(() => {
        expect(body.textContent).toContain("Permission denied");
      });
    });
  });

  describe("recent workspaces", () => {
    it("renders recent workspace entries", () => {
      const { getByText } = renderBrowser({
        recentWorkspaces: [{ path: "/recent/a" }, { path: "/recent/b" }],
      });

      expect(getByText("/recent/a")).toBeTruthy();
      expect(getByText("/recent/b")).toBeTruthy();
    });

    it("clicks a recent entry → changeWorkspace → onChanged → onClose", async () => {
      mockedChangeWorkspace.mockResolvedValue({
        success: true,
        cwd: "/recent/a",
        notice: "",
      });
      const onChanged = vi.fn();
      const onClose = vi.fn();
      const { getByText } = renderBrowser({
        recentWorkspaces: [{ path: "/recent/a" }],
        onChanged,
        onClose,
      });

      fireEvent.click(getByText("/recent/a"));

      await waitFor(() => {
        expect(mockedChangeWorkspace).toHaveBeenCalledWith("/recent/a");
        expect(onChanged).toHaveBeenCalledWith("/recent/a");
        expect(onClose).toHaveBeenCalled();
      });
    });

    it("shows error when recent workspace switch fails", async () => {
      mockedChangeWorkspace.mockResolvedValue({
        success: false,
        cwd: "",
        notice: "Dir gone",
      });
      const { getByText, body } = renderBrowser({
        recentWorkspaces: [{ path: "/gone" }],
      });

      fireEvent.click(getByText("/gone"));

      await waitFor(() => {
        expect(body.textContent).toContain("Dir gone");
      });
    });

    it("does not render recent section when list is empty", () => {
      const { body } = renderBrowser({ recentWorkspaces: [] });
      expect(body.textContent).not.toContain("Recent");
    });
  });

  describe("modal controls", () => {
    it("calls onClose when Cancel is clicked", () => {
      const onClose = vi.fn();
      const { getByText } = renderBrowser({ onClose });
      fireEvent.click(getByText("Cancel"));
      expect(onClose).toHaveBeenCalled();
    });

    it("calls onGoHome when Home is clicked", () => {
      const onGoHome = vi.fn();
      const { getByText } = renderBrowser({ onGoHome });
      fireEvent.click(getByText("Home"));
      expect(onGoHome).toHaveBeenCalled();
    });

    it("does not render when open is false", () => {
      const { container } = renderBrowser({ open: false });
      expect(container.innerHTML).toBe("");
    });
  });
});
