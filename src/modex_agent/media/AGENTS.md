# media

Concrete media implementation for the attachment system (ADR-0013): local-filesystem byte storage, magic-byte MIME classification, the perception gate, and the dangerous-executable security policy.

## Purpose

The `media/` package owns the concrete half of the attachment system. The shared contracts — `Attachment`, `Kind`, `AttachmentLocator`, the `MediaStore` ABC, `StoredFile`, `StoredMediaKind`, `MediaRefCollisionError` — live in `modex_agent/core/media.py` (plan §14.1, work package C1; see ADR-0006). This package depends on those contracts downward (`media → core` is the legal direction) and adds the concrete behavior.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Facade — exports `LocalFileMediaStore` only (concrete surface; contracts are NOT re-exported, no-shim rule) |
| `store.py` | `LocalFileMediaStore` — the local-filesystem `MediaStore`: atomic streamed saves (`.part` + `replace`), path-escape-proof `<kind>/<session_id>/<attachment_id>` layout via `safe_segment`, collision-rejecting `resolve_bytes`, oldest-by-mtime `enforce_budget` eviction (ADR-0013 §7 Layer 2) |
| `mime.py` | Magic-byte MIME sniffing (`sniff_mime`) + three-way `Kind` classification (`classify_kind`) — magic bytes authoritative, extensions fallback only (ADR-0013 §8) |
| `gate.py` | `perception_gate` — the single accept/reject authority for inbound attachments (ADR-0013 §7 Layer 1). Ordering is security-load-bearing: disguise rejection runs before type/size checks. `GateDecision` / `RejectReason` |
| `security.py` | `DANGEROUS_MAGIC` — the fixed dangerous-executable magic-byte table (PE/ELF/Mach-O), `MappingProxyType`-immutable so callers cannot neuter disguise-rejection |
| `media_utils.py` | `compress_image` + `CompressedImage` — the live compression core for model delivery (idempotent pass-through; consumed by the read tool and the LLM-boundary injection resolver) |

## For AI Agents

### Working In This Directory
- Contract types come from `modex_agent.core.media` — never define media contracts here.
- `LocalFileMediaStore` has NO workspace/pool knowledge; it operates purely on the directory it is given. Business routing (ws+pool → directory) is the resolver's job (`bot.service.media_store`).
- The gate's ordering (disguise rejection FIRST) is load-bearing — do not reorder the checks.
- Extensions are deliberately NOT gated; the magic-disguise check is the real defense (see `security.py` module docstring).

## Dependencies

### Internal
- `modex_agent.core.media` — MediaStore ABC, StoredFile, StoredMediaKind, MediaRefCollisionError, Kind
- `modex_agent.workspace.paths` — `safe_segment` for path containment
- `modex_agent.multi_agent.pool_config.media` — `MediaConfig` (gate size caps)

### External
- Pillow (optional) — `media_utils` image compression; passes bytes through unchanged when unavailable
