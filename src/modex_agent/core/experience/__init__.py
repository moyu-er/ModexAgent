"""Experience layer — reusable problem-solving patterns from past sessions."""

from .builder import ExperiencePromptBuilder
from .curator import ExperienceCurator
from .manager import ExperienceManager
from .meta import ExperienceMetaStore, PerFileExperienceMetaStore
from .models import Experience, ExperienceSummary
from .name_sync import auto_correct_frontmatter_name
from .source import FileExperienceSource, sanitize_name
from .validation import validate_experience_md

__all__ = [
    "ExperienceManager",
    "FileExperienceSource",
    "ExperienceMetaStore",
    "PerFileExperienceMetaStore",
    "ExperienceCurator",
    "ExperiencePromptBuilder",
    "validate_experience_md",
    "auto_correct_frontmatter_name",
    "sanitize_name",
    "Experience",
    "ExperienceSummary",
]
