# 01 — Inject MODEX_CONTROL_ORIGIN into agent environments

**What to build:** The bot reads its HTTP listener address from `bot_config.yml` (`webui.host` + `webui.port`) at startup, normalizes `0.0.0.0` to `127.0.0.1` for injection, and passes the resulting origin string (e.g. `http://127.0.0.1:21800`) into `ExternalEnvSpec` at pool construction time. `ExternalEnvSpec` gains a `control_origin: str` field. `ExternalEnvBuilder.build_modex_vars()` emits `MODEX_CONTROL_ORIGIN` from this field. Both the external agent spawn path (`ExternalEnvBuilder.build()`) and the native agent contextvar path (`NativeEnvInjectionHook.before_turn()`) receive the value from the single extraction point.

No user-visible behavior change. Agent processes gain a new `MODEX_CONTROL_ORIGIN` environment variable that no current code reads. This is prefactoring that enables the new HTTP-based CLI to locate the bot.

**Blocked by:** None — can start immediately.

**Status:** done (commit f34383dd)

- [x] `ExternalEnvSpec` has a `control_origin: str` field with a docstring matching ADR-0035 D6.
- [x] `ExternalEnvBuilder.build_modex_vars()` includes `MODEX_CONTROL_ORIGIN` in its output dict.
- [x] Bot startup (pool_builder or wiring) reads `webui.host`/`webui.port` from config and constructs the origin string.
- [x] `0.0.0.0` host is normalized to `127.0.0.1` for injection.
- [x] All `ExternalEnvSpec` construction sites in `examples/bot_project` populate `control_origin`.
- [x] Existing tests for `ExternalEnvBuilder` and `NativeEnvInjectionHook` pass with the new field present.
- [x] New test verifies `build_modex_vars()` output contains `MODEX_CONTROL_ORIGIN` with the expected loopback value.
