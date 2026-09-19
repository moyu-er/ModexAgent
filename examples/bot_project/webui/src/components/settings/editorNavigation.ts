import { createContext, useCallback, useContext, useEffect, useRef } from "react";
import type { NavigationGuard } from "../../hooks/useHashRoute";

/** Editors keep ownership of their draft and persistence; the page guards leaving. */
export interface SettingsEditorHandle {
  isDirty: () => boolean;
  save?: () => Promise<boolean>;
  discard?: () => void;
}

export type EditorThunk = () => SettingsEditorHandle;

export interface EditorRegistration {
  register: (read: EditorThunk) => () => void;
  requestLeave: (resume: () => void, preserved?: SettingsEditorHandle) => void;
}

export const EditorContext = createContext<EditorRegistration>({
  register: () => () => {},
  requestLeave: (resume) => resume(),
});

export function useSettingsNavigation(preserved?: SettingsEditorHandle): NavigationGuard {
  const { requestLeave } = useContext(EditorContext);
  return useCallback((resume) => requestLeave(resume, preserved), [requestLeave, preserved]);
}

/** Read the latest draft handle without re-registering on each keystroke. */
export function useRegisterSettingsEditor(makeHandle: EditorThunk): void {
  const { register } = useContext(EditorContext);
  const makeRef = useRef(makeHandle);
  makeRef.current = makeHandle;
  useEffect(() => register(() => makeRef.current()), [register]);
}
