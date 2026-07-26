# 02 — Extract workspace request resolver from WebUIServer

**What to build:** Extract the workspace-resolution behavior currently embedded in `WebUIServer._ws_root_of`, `_sessions_dir_of_ws`, and `_index_dir_of_ws` into a shared bot-owned workspace request module. Existing WebUI routes and new control routes will both use it.

The extraction preserves current WebUI behavior exactly:
- The external field name remains `ws`.
- Empty `ws` selects home.
- Relative `ws` resolves against home.
- Absolute `ws` is used directly.
- Session-index and transcript paths derive from the same resolved root.

`WebUIServer` delegates to the extracted module — no behavior change for existing WebUI routes. The shared module returns a structured resolution result that the WebUI adapter can use with its current fallback behavior, while control routes can enforce stricter validation (non-empty absolute path required).

No user-visible behavior change. This is prefactoring for control routes.

**Blocked by:** None — can start immediately.

**Status:** done (commit 038cb274)

- [x] Workspace-resolution logic extracted from `WebUIServer` into a shared module.
- [x] `WebUIServer` delegates to the extracted module for all workspace path resolution.
- [x] Existing WebUI behavior unchanged: empty=home, relative=against-home, absolute=direct.
- [x] Session-index and transcript path derivation uses the shared module.
- [x] The shared module returns a structured result (not just `Path`) so callers can distinguish resolution outcomes.
- [x] Existing WebUI tests pass without modification.
- [x] No new public API beyond the extracted module's interface.
