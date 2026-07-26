"""SkillsStore — manage the global skill library + per-agent skill assignment.

Operates on a configurable base dir (default ``examples/bot_project``-relative;
overridable per-instance for ``tmp_path`` tests).

Layout::

    local_skills/<name>/             # the global skill LIBRARY (CRUD here)
        SKILL.md
        ... arbitrary files ...
    ~/.agents/skills/<name>/          # USER global skills (read-only augment;
                                      # may themselves be links). Loaded into the
                                      # registry alongside local_skills/.
    skills/<pool>/<agent>/<name>/     # per-agent: a real copy (committed in the
                                      # repo / manually placed) OR a link created
                                      # by assign -> a global source

The library lives OUTSIDE ``skills/`` (a sibling ``local_skills/`` dir) so it
can never collide with a per-pool skill directory. Disk is the single
source of truth — no skill selection is persisted in pool.yml; the runtime
SkillManager and the WebUI both read ``skills/<pool>/<agent>/`` directly.

Two global sources, REPO PRIORITY: a name present in both ``local_skills/``
and ``~/.agents/skills/`` resolves to the repo copy (``_resolve_global_source``).
CRUD (``upload_skill`` / ``delete_skill``) targets the repo ``local_skills/``
ONLY — user-home skills are read-only here.

Per-agent assignment (``assign_skill_to_agent``) is a directory LINK to the
resolved global source: a symlink on POSIX (and on Windows when the symlink
privilege is available), falling back to a directory junction on Windows
without that privilege (no Developer Mode / admin needed). Removal
(``unassign_skill_from_agent``) handles BOTH shapes — a link is detached, a
real directory (a manual copy) is removed recursively — so a skill manually
copied into an agent root can also be deleted through the same operation.

Semantics:

* ``list_global_skills`` aggregates ``local_skills/`` + ``~/.agents/skills/``,
  deduped by name (repo wins).
* ``upload_skill`` writes a file tree under ``local_skills/<name>/`` (overwrite
  allowed on re-upload). Each relative path is normalized and must resolve
  UNDER the skill dir — traversal is rejected. Shadows a same-named user skill.
* ``delete_skill`` removes a repo ``local_skills/<name>/`` dir only. Per-agent
  links pointing at it go dangling; a same-named user skill, if any, becomes
  the resolved source instead.
* ``assign_skill_to_agent`` links the resolved source (repo or user).
* ``list_agent_skills`` marks each entry ``source=SkillSource.GLOBAL`` if it resolves to
  either global source, else ``source=SkillSource.LOCAL`` (a manually-placed copy).

All names validated by ``^[a-z][a-z0-9_-]+$`` (path-traversal guard).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from bot.config import SkillEntry, SkillOrigin, SkillSource
from modex_agent.core.frontmatter import parse_frontmatter

logger = logging.getLogger(__name__)

LOCAL_SKILLS_DIR = "local_skills"
AGENT_SKILLS_DIR = "skills"
USER_SKILLS_DIR_NAME = ".agents/skills"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]+$")


class SkillValidationError(ValueError):
    """Raised on a bad skill/pool/agent name or traversal attempt."""


def _validate_name(name: str, kind: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise SkillValidationError(f"Invalid {kind} name {name!r}: must match {_NAME_RE.pattern}")
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise SkillValidationError(f"Invalid {kind} name {name!r}: traversal")


_MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s+.*$", re.MULTILINE)
_MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]+)\]\([^)]+\)")
_MARKDOWN_EMPH_RE = re.compile(r"(\*{1,2}|_{1,2})([^*]+?)\1")
_MARKDOWN_CODE_RE = re.compile(r"`([^`]+)`")


def _clean_markdown_line(line: str) -> str:
    """Remove common Markdown formatting from a line, keeping readable text."""
    text = line
    text = _MARKDOWN_LINK_RE.sub(r"\1", text)
    text = _MARKDOWN_EMPH_RE.sub(r"\2", text)
    text = _MARKDOWN_CODE_RE.sub(r"\1", text)
    text = _MARKDOWN_HEADING_RE.sub("", text)
    return text.strip()


def _extract_first_body_paragraph(body: str) -> str:
    """Return the first non-empty paragraph of a Markdown body as plain text."""
    paragraph_lines: list[str] = []
    for line in body.splitlines():
        cleaned = _clean_markdown_line(line)
        if not cleaned:
            if paragraph_lines:
                break
            continue
        paragraph_lines.append(cleaned)
    return " ".join(paragraph_lines) if paragraph_lines else ""


def _read_skill_description(skill_dir: Path) -> str:
    """Return the human-readable description from ``skill_dir / SKILL.md``.

    Priority:
    1. YAML frontmatter ``description`` field (parsed with PyYAML via
       ``modex_agent.core.frontmatter``).
    2. First non-empty paragraph of the Markdown body, with Markdown markup
       removed so headings/link targets don't leak into the description.

    Returns the empty string when there is no SKILL.md, no extractable text,
    or the file cannot be read (bad UTF-8, permissions, etc.).
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return ""
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Cannot read SKILL.md in %s: %s", skill_dir, exc)
        return ""

    frontmatter, body = parse_frontmatter(text)
    raw_description = frontmatter.get("description", "")
    description = raw_description.strip() if isinstance(raw_description, str) else ""
    if description:
        return description

    return _extract_first_body_paragraph(body)


def _symlink_target(src: Path, dst_parent: Path) -> str:
    """Return the symlink target string for ``src`` relative to ``dst_parent``.

    Prefer a relative target so the ``skills/`` tree stays portable. When the
    two paths do not share a common root (e.g., different drives on Windows),
    ``os.path.relpath`` raises ``ValueError``; fall back to an absolute target.
    """
    try:
        return os.path.relpath(src, dst_parent)
    except ValueError:
        return str(src)


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
    link_target = _symlink_target(src_abs, dst_abs.parent)
    try:
        os.symlink(link_target, dst_abs, target_is_directory=True)
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
        self.skills_dir: Path = self.base_dir / AGENT_SKILLS_DIR
        # The repo skill library lives OUTSIDE the per-pool ``skills/`` tree
        # (a sibling ``local_skills/`` dir) so it can never collide with a pool.
        self.local_dir: Path = self.base_dir / LOCAL_SKILLS_DIR
        # User-installed global skills default to ``~/.agents/skills/`` but are
        # injectable (mainly for tests). ``None`` resolves lazily via the
        # ``user_global_dir`` property so ``Path.home()`` is read at use time.
        self._user_global_dir_override: Path | None = user_global_dir

    @property
    def user_global_dir(self) -> Path:
        """User-installed global skills: ``~/.agents/skills/`` (cross-platform).

        Read-only augmentation of the library: skills here are loaded into the
        global registry alongside ``local_skills/``. On a name clash the REPO
        ``local_skills/`` copy wins (see ``_resolve_global_source``). The dir
        itself need not exist; if it is missing, it is simply skipped.
        """
        if self._user_global_dir_override is not None:
            return self._user_global_dir_override
        return Path(f"~/{USER_SKILLS_DIR_NAME}").expanduser()

    # ─── global skills ──────────────────────────────────────────────────────

    def _local_skill_dir(self, name: str) -> Path:
        """The REPO-side skill dir (CRUD target). Always ``local_skills/<name>``."""
        _validate_name(name, "skill")
        return self.local_dir / name

    def _resolve_global_source(self, name: str) -> Path | None:
        """Resolve a global skill name to its source dir, REPO PRIORITY.

        Returns ``local_skills/<name>`` if it exists, else the user-home
        ``~/.agents/skills/<name>`` if that exists, else ``None``. Used by
        assign + existence checks so a name present in both sources always
        resolves to the repo copy. User-home skills may themselves be links —
        the path is returned as-is (the per-agent link points at it).
        """
        _validate_name(name, "skill")
        repo = self.local_dir / name
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

    def _origin_for_source(self, src: Path | None) -> SkillOrigin | None:
        """Return the origin label for a resolved global skill source."""
        if src is None:
            return None
        if src.parent.resolve() == self.local_dir.resolve():
            return SkillOrigin.REPO
        if src.parent.resolve() == self.user_global_dir.resolve():
            return SkillOrigin.USER
        logger.warning("Unrecognized skill origin for %s (parent: %s)", src, src.parent)
        return None

    def list_global_skills(self) -> list[SkillEntry]:
        """List every global skill: repo ``local_skills/`` PLUS user-home
        ``~/.agents/skills/``, deduped by name with REPO PRIORITY.

        A name present in both appears once. Each entry is ``source=SkillSource.GLOBAL``
        and carries a ``description`` parsed from the resolved source's
        ``SKILL.md`` (repo copy wins over user-home copy).
        """
        seen: set[str] = set()
        order: list[str] = []
        for source_dir in (self.local_dir, self.user_global_dir):
            if not source_dir.exists():
                continue
            for entry in sorted(source_dir.iterdir()):
                if entry.name in seen:
                    continue
                if self._dir_exists_following_links(entry):
                    order.append(entry.name)
                    seen.add(entry.name)
        out: list[SkillEntry] = []
        for name in order:
            src = self._resolve_global_source(name)
            if src is None:
                continue
            out.append(
                SkillEntry(
                    name=name,
                    source=SkillSource.GLOBAL,
                    origin=self._origin_for_source(src),
                    description=_read_skill_description(src),
                )
            )
        return out

    def upload_skill(self, name: str, file_tree: dict[str, bytes | str]) -> SkillEntry:
        """Write a file tree under ``local_skills/<name>/`` (repo library only).

        ``file_tree`` maps relative paths (within ``<name>/``) → contents
        (bytes or str, UTF-8). Overwrite of an existing skill is allowed.
        Each relative path is normalized and rejected if it escapes the
        skill dir. The skill dir is recreated from scratch on each upload
        so deletions in the tree propagate. Uploading shadows a same-named
        user-home skill (repo wins).
        """
        _validate_name(name, "skill")
        skill_dir = self._local_skill_dir(name)
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        skill_dir.mkdir(parents=True, exist_ok=True)
        for rel, content in file_tree.items():
            self._write_under(skill_dir, rel, content)
        return SkillEntry(
            name=name,
            source=SkillSource.GLOBAL,
            origin=SkillOrigin.REPO,
            description=_read_skill_description(skill_dir),
        )

    def delete_skill(self, name: str) -> bool:
        """Remove ``local_skills/<name>/`` (repo library only).

        Returns ``True`` if the repo skill existed. Per-agent links pointing at
        it go dangling; a same-named user-home skill, if any, becomes the
        resolved source instead (so deletion does NOT strand the skill when a
        user copy exists). User-home skills are never deleted here.
        """
        skill_dir = self._local_skill_dir(name)
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
        ``local_skills/<name>`` first, then user-home ``~/.agents/skills/<name>``).
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
        ``source=SkillSource.GLOBAL`` if it ALSO exists in either global source (repo
        ``local_skills/`` or user-home ``~/.agents/skills/``), else
        ``source=SkillSource.LOCAL``. For global-backed entries, ``origin`` is ``"repo"``
        when the resolved source is the repo library, or ``"user"`` when it is
        the user-home directory.
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
            src = self._resolve_global_source(entry.name)
            is_global = src is not None
            out.append(
                SkillEntry(
                    name=entry.name,
                    source=SkillSource.GLOBAL if is_global else SkillSource.LOCAL,
                    origin=self._origin_for_source(src),
                    description=_read_skill_description(entry),
                )
            )
        return out

    def clear_pool_skills(self, pool: str) -> bool:
        """Remove every per-agent skill assignment under ``skills/<pool>/``.

        No-ops (returns ``False``) when the pool has no skill directory. Only
        the per-pool ``skills/`` subtree is touched — the global libraries
        (``local_skills/`` and ``~/.agents/skills/``) are never modified.
        """
        _validate_name(pool, "pool")
        src = self.skills_dir / pool
        if not src.exists():
            return False
        shutil.rmtree(src)
        return True

    # ─── helpers ────────────────────────────────────────────────────────────

    def _write_under(self, root: Path, rel: str, content: bytes | str) -> None:
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
            raise SkillValidationError(f"Relative path {rel!r} escapes skill dir {root}") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
