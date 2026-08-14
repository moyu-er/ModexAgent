from __future__ import annotations

from bot.kb.models import KbFilter


def build_filter_clauses(
    filter: KbFilter,
    *,
    alias: str = "",
) -> tuple[list[str], list[str]]:
    """Build three-state filter WHERE clauses.

    None = skip (global); "" = public-only; "value" = isolated.
    alias: optional table alias prefix (e.g. "e." for JOINs).
    """
    clauses: list[str] = []
    params: list[str] = []
    if filter.task_id is not None:
        clauses.append(f"{alias}task_id = ?")
        params.append(filter.task_id)
    if filter.session_id is not None:
        clauses.append(f"{alias}session_id = ?")
        params.append(filter.session_id)
    if filter.category is not None:
        clauses.append(f"{alias}category = ?")
        params.append(filter.category)
    return clauses, params
