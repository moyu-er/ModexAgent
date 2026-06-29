## ADDED Requirements

### Requirement: MediaStore persists inbound bytes per workspace+pool

The framework SHALL provide a `MediaStore` ABC with `save`, `read`, `delete`,
`list`, and `enforce_budget` operations, and a `LocalFileMediaStore` implementation.
Inbound attachment bytes SHALL be persisted under
`<workspace_root>/<data_dir>/media/<pool>/uploads/<session_id>/`. Only inbound bytes
flow through `MediaStore`; outbound reads the filesystem directly.

#### Scenario: inbound file saved under the session's media directory
- **WHEN** an inbound attachment is saved for session `{conv}.main` in pool `p` under workspace root `R`
- **THEN** the bytes SHALL be stored under `R/<data_dir>/media/p/uploads/{conv}.main/<attachment_id>` and `save` SHALL return the stored path

#### Scenario: outbound never touches MediaStore
- **WHEN** an outbound (agent-produced) attachment is recorded
- **THEN** no byte SHALL be copied into the media directory and the download path SHALL read the file at its original filesystem location

### Requirement: MediaStore ABC + LocalFileMediaStore live in the framework; routing lives in the business layer

The framework SHALL provide the `MediaStore` ABC and `LocalFileMediaStore`, both
operating on a resolved directory (no workspace/pool knowledge). The
per-(workspace,pool) **resolver** SHALL live in the **business** layer, mirroring
the business `WorkspaceScopedTranscriptStore` (which already co-locates with the
WebUI's workspace-root resolution). The resolver SHALL resolve the workspace root
the same way the WebUI does and build the media directory via the framework
`WorkspacePaths.media_dir(pool)` primitive. No workspace-root resolver keyed by an
incoming `ws` string SHALL be introduced into the framework.

#### Scenario: two pools in one workspace get distinct media directories
- **WHEN** two pools `a` and `b` exist in workspace `R`
- **THEN** the resolver SHALL produce distinct directories `R/<data_dir>/media/a/` and `R/<data_dir>/media/b/`, each handed to a `LocalFileMediaStore`

#### Scenario: the framework store has no workspace/pool coupling
- **WHEN** a `LocalFileMediaStore` is constructed
- **THEN** it SHALL operate purely on the directory it is given, with no reference to workspace ids, pool maps, or `ws` strings

### Requirement: WorkspacePaths exposes a media_dir primitive

`WorkspacePaths` SHALL expose `media_dir(pool) -> Path`, routed through the existing
escape-proof `_child` sanitizer, so media paths cannot escape the workspace root.

#### Scenario: media_dir is contained within the workspace root
- **WHEN** `WorkspacePaths(root=R).media_dir("p")` is called
- **THEN** the result SHALL resolve to `R/<data_dir>/media/p` and be contained by `R`

### Requirement: save and read are stream/path-oriented

`MediaStore.save` SHALL accept a stream (not require the whole file in memory) and
`MediaStore.read` SHALL return a path or stream (not buffer the whole file). This
SHALL hold up to the largest configured file (1 GB outbound-equivalent reads).

#### Scenario: a large file is saved without whole-file buffering
- **WHEN** a stream of a large file is saved via `MediaStore.save`
- **THEN** the implementation SHALL NOT read the entire file into a single in-memory buffer

### Requirement: MediaStore backend is swappable to object storage

`MediaStore` SHALL be an ABC so a future object-storage (S3-class) backend can be
substituted behind the same interface, with `LocalFileMediaStore` as the current
implementation.

#### Scenario: a second backend implements the same ABC
- **WHEN** an object-storage backend is provided
- **THEN** it SHALL be usable wherever `MediaStore` is expected, without changes to callers
