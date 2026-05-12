"""Skill configuration."""

from pydantic import BaseModel


class SkillsConfig(BaseModel):
    """Agent skill configuration. None = no skills loaded.

    roots:
        Directories containing SKILL.md subdirectories.
        Each subdirectory with a SKILL.md is auto-discovered.
        Runtime new subdirectories are picked up on reload.
    allowed:
        Optional skill name whitelist. None = all skills available.
    """

    roots: list[str] = []
    allowed: list[str] | None = None
