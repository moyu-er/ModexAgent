"""ReAct runtime hooks — turn-lifecycle behavior owned by the ReAct domain.

Moved here from ``hook/builtin/`` (W2 domain-ownership wave): every module in
this package runtime-imports ReAct turn state (``get_react_state``) or the
native-agent env seam, so its implementation home is the ReAct package while
registration still flows through the HOOK-slot factories in
``plugins/defaults/hooks.py``.
"""
