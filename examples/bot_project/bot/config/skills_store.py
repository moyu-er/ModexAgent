"""SkillsStore — manage the global skill library + per-agent skill assignment.

Operates on a configurable base dir (default ``examples/bot_project``-relative;
overridable per-instance for ``tmp_path`` tests).

Layout::

    global_skills/<name>/             # the global skill LIBRARY (CRUD here)
        SKILL.md
        ... arbitrary files ...
    ~/.agents/skills/<name>/          # USER global skills (read-only augment;
                                      # may themselves be links). Loaded into the
                                      # registry alongside global_skills/.
    skills/<pool>/<agent>/<name>/     # per-agent: a real copy (committed in the
                                      # repo / manually placed) OR a link created
                                      # by assign -> a global source

The library lives OUTSIDE ``skills/`` (a sibling ``global_skills/`` dir) so it
can never collide with a pool literally named "global". Disk is the single
source of truth — no skill selection is persisted in pool.yml; the runtime
SkillManager and the WebUI both read ``skills/<pool>/<agent>/`` directly.

Two global sources, REPO PRIORITY: a name present in both ``global_skills/``
and ``~/.agents/skills/`` resolves to the repo copy (``_resolve_global_source``).
CRUD (``upload_skill`` / ``delete_skill``) targets the repo ``global_skills/``
ONLY — user-home skills are read-only here.

Per-agent assignment (``assign_skill_to_agent``) is a directory LINK to the
resolved global source: a symlink on POSIX (and on Windows when the symlink
privilege is available), falling back to a directory junction on Windows
without that privilege (no Developer Mode / admin needed). Removal
(``unassign_skill_from_agent``) handles BOTH shapes — a link is detached, a
real directory (a manual copy) is removed recursively — so a skill manually
copied into an agent root can also be deleted through the same operation.

Semantics:

* ``list_global_skills`` aggregates ``global_skills/`` + ``~/.agents/skills/``,
  deduped by name (repo wins).
* ``upload_skill`` writes a file tree under ``global_skills/<name>/`` (overwrite
  allowed on re-upload). Each relative path is normalized and must resolve
  UNDER the skill dir — traversal is rejected. Shadows a same-named user skill.
* ``delete_skill`` removes a repo ``global_skills/<name>/`` dir only. Per-agent
  links pointing at it go dangling; a same-named user skill, if any, becomes
  the resolved source instead.
* ``assign_skill_to_agent`` links the resolved source (repo or user).
* ``list_agent_skills`` marks each entry ``source="global"`` if it resolves to
  either global source, else ``source="local"`` (a manually-placed copy).

All names validated by ``^[a-z][a-z0-9_-]+$`` (path-traversal guard).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from bot.config.pool_payloads import SkillEntry

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]+$")


class SkillValidationError(ValueError):
    """Raised on a bad skill/pool/agent name or traversal attempt."""


def _validate_name(name: str, kind: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise SkillValidationError(
            f"Invalid {kind} name {name!r}: must match {_NAME_RE.pattern}"
        )
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise SkillValidationError(f"Invalid {kind} name {name!r}: traversal")


def _create_dir_link(src: Path, dst: Path) -> None:
    """Create a directory link at ``dst`` pointing to ``src``.

    Single converged seam — every caller (assign, migrate) goes through here.
    No privilege is required on any platform:

    * Try a portable symlink first (target stored RELATIVE to ``dst``'s parent,
      so the whole ``skills/`` tree stays portable and git versions it as a true
      symlink). This is the preferred form and the only form on POSIX.
    * On Windows without the symlink privilege (``WinError 1314`` — no Developer
      Mode, non-admin), fall back to a directory junction via ``mklink /J``.
      Junctions need NO privilege, are transparent to the runtime / git /
      Explorer, and require an absolute target.

    ``src`` must exist; ``dst`` must not.
    """
    src_abs = src.resolve()
    dst_abs = dst.resolve()
    rel = os.path.relpath(src_abs, dst_abs.parent)
    try:
        os.symlink(rel, dst_abs, target_is_directory=True)
        return
    except OSError as exc:
        # Only the symlink-privilege error falls back to a junction; anything
        # else is a real failure. Non-Windows has no fallback.
        if os.name != "nt" or getattr(exc, "winerror", None) != 1314:
            raise
    _create_junction(src_abs, dst_abs)


def _create_junction(src_abs: Path, dst_abs: Path) -> None:
    """Windows directory junction via ``mklink /J`` — no privilege required.

    ``cmd`` is invoked with a literal argument list (``shell=False``), so there
    is no shell-injection surface. Junctions need an absolute target.
    """
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(dst_abs), str(src_abs)],
        shell=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not dst_abs.exists():
        raise SkillValidationError(
            f"Failed to create junction {dst_abs} -> {src_abs}: "
            f"rc={proc.returncode} stderr={proc.stderr.strip()}"
        )


# FILE_ATTRIBUTE_REPARSE_POINT — a junction/mount-point sets this but is NOT a
# symlink (Path.is_symlink() returns False for it).
_REPARSE_POINT = 0x400


def _is_reparse_point(path: Path) -> bool:
    """True if ``path`` is a reparse point that is not a true symlink (a junction).

    Only meaningful on Windows; on POSIX there are no non-symlink reparse points
    in this layout, so it always returns False.
    """
    if os.name != "nt":
        return False
    try:
        attrs = path.lstat().st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attrs & _REPARSE_POINT)


class SkillsStore:
    """Manage global skills + per-agent links. Plain runtime class (base_dir)."""

    def __init__(
        self,
        base_dir: Path | None = None,
        *,
        user_global_dir: Path | None = None,
    ) -> None:
        self.base_dir: Path = Path(base_dir) if base_dir is not None else Path(".")
        self.skills_dir: Path = self.base_dir / "skills"
        # The global skill library lives OUTSIDE the per-pool ``skills/`` tree
        # (a sibling ``global_skills/`` dir) so it can never collide with a pool
        # literally named "global".
        self.global_dir: Path = self.base_dir / "global_skills"
        # User-installed global skills default to ``~/.agents/skills/`` but are
        # injectable (mainly for tests). ``None`` resolves lazily via the
        # ``user_global_dir`` property so ``Path.home()`` is read at use time.
        self._user_global_dir_override: Path | None = user_global_dir

    @property
    def user_global_dir(self) -> Path:
        """User-installed global skills: ``~/.agents/skills/`` (cross-platform).

        Read-only augmentation of the library: skills here are loaded into the
        global registry alongside ``global_skills/``. On a name clash the REPO
        ``global_skills/`` copy wins (see ``_resolve_global_source``). The dir
        itself need not exist.
        """
        if self._user_global_dir_override is not None:
            return self._user_global_dir_override
        return Path.home() / ".agents" / "skills"

    # ─── global skills ──────────────────────────────────────────────────────

    def _global_skill_dir(self, name: str) -> Path:
        """The REPO-side global skill dir (CRUD target). Always ``global_skills/<name>``."""
        _validate_name(name, "skill")
        return self.global_dir / name

    def _resolve_global_source(self, name: str) -> Path | None:
        """Resolve a global skill name to its source dir, REPO PRIORITY.

        Returns ``global_skills/<name>`` if it exists, else the user-home
        ``~/.agents/skills/<name>`` if that exists, else ``None``. Used by
        assign + existence checks so a name present in both sources always
        resolves to the repo copy. User-home skills may themselves be links —
        the path is returned as-is (the per-agent link points at it).
        """
        _validate_name(name, "skill")
        repo = self.global_dir / name
        if self._dir_exists_following_links(repo):
            return repo
        user = self.user_global_dir / name
        if self._dir_exists_following_links(user):
            return user
        return None

    @staticmethod
    def _dir_exists_following_links(path: Path) -> bool:
        """``path`` exists AND is a directory, following symlinks/junctions.

        ``Path.exists`` already follows links; this just coalesces the dir check
        so callers treat a user-side linked skill as a usable source.
        """
        try:
            return path.is_dir()
        except OSError:
            return False

    def list_global_skills(self) -> list[SkillEntry]:
        """List every global skill: repo ``global_skills/`` PLUS user-home
        ``~/.agents/skills/``, deduped by name with REPO PRIORITY.

        A name present in both appears once. Each entry is ``source="global"``.
        """
        seen: set[str] = set()
        out: list[SkillEntry] = []
        for source_dir in (self.global_dir, self.user_global_dir):
            if not source_dir.exists():
                continue
            for entry in sorted(source_dir.iterdir()):
                if entry.name in seen:
                    continue
                if self._dir_exists_following_links(entry):
                    out.append(SkillEntry(name=entry.name, source="global"))
                    seen.add(entry.name)
        return out

    def upload_skill(
        self, name: str, file_tree: dict[str, bytes | str]
    ) -> SkillEntry:
        """Write a file tree under ``global_skills/<name>/`` (repo library only).

        ``file_tree`` maps relative paths (within ``<name>/``) → contents
        (bytes or str, UTF-8). Overwrite of an existing skill is allowed.
        Each relative path is normalized and rejected if it escapes the
        skill dir. The skill dir is recreated from scratch on each upload
        so deletions in the tree propagate. Uploading shadows a same-named
        user-home skill (repo wins).
        """
        _validate_name(name, "skill")
        skill_dir = self._global_skill_dir(name)
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        skill_dir.mkdir(parents=True, exist_ok=True)
        for rel, content in file_tree.items():
            self._write_under(skill_dir, rel, content)
        return SkillEntry(name=name, source="global")

    def delete_skill(self, name: str) -> bool:
        """Remove ``global_skills/<name>/`` (repo library only).

        Returns ``True`` if the repo skill existed. Per-agent links pointing at
        it go dangling; a same-named user-home skill, if any, becomes the
        resolved source instead (so deletion does NOT strand the skill when a
        user copy exists). User-home skills are never deleted here.
        """
        skill_dir = self._global_skill_dir(name)
        if not skill_dir.exists():
            return False
        shutil.rmtree(skill_dir)
        return True

    # ─── per-agent links ────────────────────────────────────────────────────

    def _agent_skill_dir(self, pool: str, agent: str, name: str) -> Path:
        _validate_name(pool, "pool")
        _validate_name(agent, "agent")
        _validate_name(name, "skill")
        return self.skills_dir / pool / agent / name

    def _remove_link(self, dst: Path) -> None:
        """Remove a per-agent link/copy: symlink, Windows junction, or real dir.

        A true symlink detaches with ``unlink``. A Windows directory junction
        reports ``is_symlink() == False`` but is a reparse point — it MUST be
        detached with ``rmdir`` (removes the link only, never the global target
        contents; ``shutil.rmtree`` refuses and would otherwise be catastrophic).
        A real directory (legacy copy) is removed recursively.
        """
        if dst.is_symlink():
            dst.unlink()
            return
        if _is_reparse_point(dst):
            os.rmdir(dst)  # junction / mount-point reparse — detach only
            return
        shutil.rmtree(dst)

    def assign_skill_to_agent(self, pool: str, agent: str, name: str) -> Path:
        """Link a global skill → ``skills/<pool>/<agent>/<name>``.

        The source is resolved by ``_resolve_global_source`` (repo
        ``global_skills/<name>`` first, then user-home ``~/.agents/skills/<name>``).
        Replaces an existing link/copy at the destination. Raises if no global
        source exists. Uses a directory link (symlink, or Windows junction
        without the symlink privilege) — the skill is never copied.
        """
        src = self._resolve_global_source(name)
        if src is None:
            raise SkillValidationError(f"Global skill {name!r} does not exist")
        dst = self._agent_skill_dir(pool, agent, name)
        if dst.exists() or dst.is_symlink():
            self._remove_link(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        _create_dir_link(src, dst)
        return dst

    def unassign_skill_from_agent(self, pool: str, agent: str, name: str) -> bool:
        """Remove ``skills/<pool>/<agent>/<name>``. Returns ``True`` if present.

        Only the per-agent link is removed — the global source is untouched.
        """
        dst = self._agent_skill_dir(pool, agent, name)
        if not dst.exists() and not dst.is_symlink():
            return False
        self._remove_link(dst)
        return True

    def list_agent_skills(self, pool: str, agent: str) -> list[SkillEntry]:
        """List skills for one agent, marking each as global-backed or local.

        A skill entry under ``skills/<pool>/<agent>/`` (a link or a real dir) is
        ``source="global"`` if it ALSO exists in either global source (repo
        ``global_skills/`` or user-home ``~/.agents/skills/``), else
        ``source="local"`` (a manually-placed copy with no global counterpart).
        """
        _validate_name(pool, "pool")
        _validate_name(agent, "agent")
        agent_root = self.skills_dir / pool / agent
        if not agent_root.exists():
            return []
        out: list[SkillEntry] = []
        for entry in sorted(agent_root.iterdir()):
            if not entry.is_dir():
                continue
            is_global = self._resolve_global_source(entry.name) is not None
            out.append(
                SkillEntry(name=entry.name, source="global" if is_global else "local")
            )
        return out

    # ─── helpers ────────────────────────────────────────────────────────────

    def _write_under(
        self, root: Path, rel: str, content: bytes | str
    ) -> None:
        """Write ``content`` to ``root / normalize(rel)``, refusing traversal.

        ``rel`` is a POSIX-style relative path (``a/b.md``). It must normalize
        to a path that stays strictly under ``root``. Backslashes are treated
        as forward slashes; a leading ``./`` prefix is stripped (but NOT a
        leading ``.`` that is itself a ``..`` traversal).
        """
        from pathlib import PurePosixPath

        rel_clean = rel.replace("\\", "/")
        # Strip a single leading "./" prefix only (not a character set).
        if rel_clean.startswith("./"):
            rel_clean = rel_clean[2:]
        if not rel_clean or rel_clean.startswith("/"):
            raise SkillValidationError(f"Unsafe relative path in skill tree: {rel!r}")
        parts = PurePosixPath(rel_clean).parts
        if any(p in {".."} for p in parts):
            raise SkillValidationError(f"Unsafe relative path in skill tree: {rel!r}")
        if any(p in {"."} for p in parts):
            # Stray '.' segments are noise — drop them.
            parts = tuple(p for p in parts if p != ".")
        target = (root.joinpath(*parts)).resolve()
        root_resolved = root.resolve()
        try:
            target.relative_to(root_resolved)
        except ValueError as exc:
            raise SkillValidationError(
                f"Relative path {rel!r} escapes skill dir {root}"
            ) from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
