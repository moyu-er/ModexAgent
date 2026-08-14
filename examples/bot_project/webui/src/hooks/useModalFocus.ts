// useModalFocus — modal focus trap shared by RunGraphModal and
// NewInstanceModal.
//
// Behavior contract (identical for both consumers):
// - On mount: capture `document.activeElement`, then focus
//   `initialFocusRef.current` when provided, else the dialog itself.
// - Esc closes via `onClose`. The keydown listener runs on the window bubble
//   phase so an Esc handled deeper (e.g. a nested DropdownPanel closing its
//   own listbox with stopPropagation) does not also close the modal.
// - Tab / Shift+Tab wrap focus within the dialog in both directions; with no
//   focusable elements, preventDefault keeps focus inside.
// - On unmount: restore focus to the element captured at mount.
//
// `onClose` must be a stable (useCallback) reference — it is the effect's
// effective dependency; an inline closure would tear down/re-add the keydown
// listener on every parent re-render and bounce focus.

import { useEffect, type RefObject } from "react";

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

export interface UseModalFocusOptions {
  dialogRef: RefObject<HTMLDivElement | null>;
  onClose: () => void;
  initialFocusRef?: RefObject<HTMLElement | null>;
}

export function useModalFocus({
  dialogRef,
  onClose,
  initialFocusRef,
}: UseModalFocusOptions): void {
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const previouslyFocused =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    if (initialFocusRef?.current) {
      initialFocusRef.current.focus();
    } else {
      dialog.focus();
    }
    const onKeyDown = (e: globalThis.KeyboardEvent): void => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== "Tab") return;
      const focusable =
        dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR);
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) {
        e.preventDefault();
        return;
      }
      const active = document.activeElement;
      if (e.shiftKey) {
        if (active === first || !dialog.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else if (active === last || !dialog.contains(active)) {
        e.preventDefault();
        first.focus();
      }
    };
    // Bubble phase so an Esc handled deeper (e.g. the deliver DropdownPanel
    // closing its own listbox) does not also close the modal.
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      previouslyFocused?.focus();
    };
  }, [dialogRef, onClose, initialFocusRef]);
}
