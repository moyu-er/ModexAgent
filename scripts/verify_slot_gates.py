#!/usr/bin/env python
"""Slot-rationalization gate verifier - mechanical enforcement of the ledgers.

Enforces every removal/convergence gate of
`.omo/plans/slot-rationalization-steps.md` section 1 (removal ledger L1-L6)
and section 2 (convergence ledger C1-C5) for the 8-wave slot rationalization
refactor (plugin slots 13 -> 10). Each wave proves its removal/convergence
claims by running a `--gate` subset per commit; `--check` over all gates
(the 16 slot-rationalization gates + the 2 capability-bundles gates below)
is the standing full battery. Completeness is proven by script, not by
eyeball.

Gate -> wave when it goes green (plan section 3 commit sequence):

    L1a-L1d   W1 (commit 1.3)          L2a-L2c   W1 (commit 1.4)
    L3a-L3c   W1 (commits 1.1/1.2; L3b docs/ scope fully clears at W7.3)
    L4        W1.4                     L5        W5.1
    L6        W4.3                     C1a/C1b   W4.2
    C2        W5.2
    G-CAP1/G-CAP2  W4 (capability-bundles todo 20)

Capability-bundles gates (W4, todo 20 of
`.omo/plans/capability-bundles-implementation.md`; SPEC §15 OQ5 + §13 W6):

    * G-CAP1 anchors the compile-time-slot asymmetry: CAPABILITY is the
      ONLY slot the scope compiler may resolve (every other slot is
      late-binding at assembly time). Inside ``src/modex_agent/scope/``
      the sanctioned faces are ``registry.resolve_capability(name)``
      (the typed CAPABILITY accessor) and
      ``registry.names(ComponentSlot.CAPABILITY)`` (the C0 enumeration).
      The pattern bans any direct ``registry.resolve(`` call (even with
      a variable slot) and any ``ComponentSlot.<OTHER>`` member access.
    * G-CAP2 anchors the W6 unconditional-injection death: within the
      assembly paths (``src/modex_agent/plugins/assembly/``,
      ``multi_agent/template.py``, and the bot project's
      ``bot/service/``) the ONLY sanctioned ``hook_runner.add(`` /
      ``add_cleanup_hook(`` / ``register_tree_aware_hooks(`` sites are
      the allowlisted dispatch/wiring functions. The allowlist is
      DESIGNED TO SHRINK: T23 (W6 glue sweep) removes the
      ``register_tree_aware_hooks`` calls in template.py and
      pipeline_wiring.py — delete those entries and lower
      ``expected_allowed_hits`` when it lands (the gate gets stronger).
    * Both gates were proven red-able at landing time (E7 discipline):
      a planted violation in each scope produced a FAIL before the
      clean tree produced a PASS — transcripts in
      `.omo/evidence/task-20-capability-bundles-implementation.txt`.

Spec source / errata:
    The GATES table below is the operative spec. Five gates are refined vs
    the plan's literal grep text, per the "GATE SPEC ERRATA" section of
    `.omo/notepads/slot-rationalization-steps/learnings.md`:

    * L5 uses `\\bmax_messages\\b` - a plain substring would false-positive on
      fork_max_messages / max_messages_per_flush / snapshot_max_messages.
    * L2c uses `\\.skills\\b|SKILL_SOURCE|\\bskills\\??\\s*:` scoped to
      pool.ts - a plain `skills` would false-positive on header comments
      referencing the disk-skill system (skillsApi.ts etc.), which SURVIVES
      the refactor. The `\\bskills\\??\\s*:` alternative catches both the
      optional (`skills?:`) and required (`skills:`) property forms, so a
      reintroduced field fails the gate either way.
    * L1a uses `MEMORY_PROVIDER|register_memory_provider|memory_provider_configs|\\bmemory_providers\\b` -
      the `\\bmemory_providers\\b` alternative (F2 hardening) catches bare
      `memory_providers` declarations the dict-key/attribute-only L1b forms
      would miss.
    * L2b uses `["']skills["']|skills:` - the plan's literal `\\.skills\\b`
      (attribute-access form) matches nothing in pool_config/: every
      `skills` usage there is a dict-key or field-declaration form
      (specs.py field decl, store.py quoted keys), so the old pattern was a
      vacuous green at baseline. Word-form correction, same learnings.md
      errata section (W0.1 baseline finding).
    * C2 allows `removeprefix("+")` in EXACTLY ONE place,
      `src/modex_agent/scope/derivation.py` — the shared derivation core
      the legacy spec-builder road's helpers moved to (ticket 11).
    * C1b keeps its allowed-files set as DATA (provisional) - the orchestrator
      may amend it after the W0.4 investigation of core.py:223/builders.py:287.

    F2 final-review hardening (2026-08-20) - five refinements on top of the
    errata above:

    * L1c scans the full ``examples/`` tree (alongside
      ``src/modex_agent/plugins``) - the plan's L1 gate specifies the whole
      examples/ scope, not just the two bot_project subdirs the gate
      previously covered (F2 re-review; zero ``\bMemoryProvider\b`` hits
      in examples/**/*.py at widening time).
    * C1b scope now includes ``examples/bot_project/plugins`` - the gate
      previously never scanned its own allowlisted factory file, so a FOURTH
      ``BotModelProvider(`` construction under plugins/ would have passed
      unnoticed.
    * C1b pins ``expected_allowed_hits=4`` - exactly the four justified
      sites (model_provider.py class definition, bot_strategies.py
      ``bot_default`` factory, core.py bot-global provider, and the eval
      harbor entry's trial-local ``bot_default`` factory).
    * L1a pattern adds ``\\bmemory_providers\\b`` - catches bare
      ``memory_providers`` declarations that the dict-key/attribute forms
      (L1b) would miss.
    * L2c pattern is ``\\.skills\\b|SKILL_SOURCE|\\bskills\\??\\s*:`` - the
      old optional-only ``skills\\?:`` form would miss a reintroduced
      REQUIRED ``skills: string[]`` property.
    * Unreadable files FAIL the gate (recorded as scope errors, in addition
      to the stderr warning) - previously they were silently unscanned, an
      under-match hole against the missing-scope fail-closed discipline.

Scan discipline:
    Traversal prunes any directory named: .venv venv node_modules __pycache__
    .git .mypy_cache .ruff_cache .pytest_cache .codegraph .omo runtime_state
    logs subworkspace dist .agents scripts. `scripts/` itself is NEVER scanned
    (this script's own gate patterns would self-match); a `web/dist` path is
    covered by the `dist` name rule. Symlinked directories are not followed
    (grep -r semantics). Unreadable files warn on stderr AND fail the gate
    (recorded as a scope error - an unscanned file cannot prove absence of
    hits).

Usage:
    python scripts/verify_slot_gates.py --baseline
    python scripts/verify_slot_gates.py --check
    python scripts/verify_slot_gates.py --check --gate L1a,L1b
    # --gate is repeatable and/or comma-separated
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".git",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".codegraph",
        ".omo",
        "runtime_state",
        "logs",
        "subworkspace",
        "dist",
        ".agents",
        "scripts",
    }
)

MAX_SAMPLES = 5
MAX_SAMPLE_LINE = 100


@dataclass(frozen=True)
class GateHit:
    """One regex match: repo-relative posix path, 1-based line number, raw text."""

    rel_path: str
    line_no: int
    line_text: str


@dataclass(frozen=True)
class Gate:
    """One gate: pattern x scope x pass rule.

    Pass rule:
      * No ``allowed_files``: pass iff zero hits.
      * With ``allowed_files``: pass iff zero hits OUTSIDE the allowed files.
        When ``expected_allowed_hits`` is set, the allowed-file hit count must
        equal it exactly (C2 single-home rule; C1b justified-sites count).

    Scopes are repo-relative. ``scope_dirs`` are walked recursively (matching
    ``suffixes``, excluded dir names pruned); ``scope_files`` are individual
    files scanned unconditionally.
    """

    gate_id: str
    pattern: str
    scope_dirs: tuple[str, ...]
    suffixes: tuple[str, ...]
    wave: str
    scope_files: tuple[str, ...] = ()
    allowed_files: tuple[str, ...] = ()
    expected_allowed_hits: int | None = None


@dataclass(frozen=True)
class GateReport:
    """Evaluation outcome for one gate."""

    gate: Gate
    hits: tuple[GateHit, ...]
    disallowed: tuple[GateHit, ...]
    passed: bool
    scope_errors: tuple[str, ...]


GATES: tuple[Gate, ...] = (
    # --- L1: MEMORY_PROVIDER slot machinery + ABC relocation (W1.3) ---
    Gate(
        gate_id="L1a",
        pattern=r"MEMORY_PROVIDER|register_memory_provider|memory_provider_configs|\bmemory_providers\b",
        scope_dirs=("src", "examples", "tests"),
        suffixes=(".py",),
        wave="W1.3",
    ),
    Gate(
        gate_id="L1b",
        pattern=r"\.memory_providers|spec\.memory_providers",
        scope_dirs=("src", "examples", "tests"),
        suffixes=(".py",),
        wave="W1.3",
    ),
    Gate(
        gate_id="L1c",
        pattern=r"\bMemoryProvider\b",
        scope_dirs=("src/modex_agent/plugins", "examples"),
        suffixes=(".py",),
        wave="W1.3",
    ),
    Gate(
        gate_id="L1d",
        pattern=r"from modex_agent\.plugins import",
        scope_dirs=("src/modex_agent/memory",),
        suffixes=(".py",),
        wave="W1.3",
    ),
    # --- L2: SKILL_SOURCE slot machinery + SubagentSpec.skills + WebUI (W1.4) ---
    Gate(
        gate_id="L2a",
        pattern=r"SKILL_SOURCE|register_skill_source|skill_source_configs|skill_sources",
        scope_dirs=("src", "examples", "tests"),
        suffixes=(".py",),
        wave="W1.4",
    ),
    Gate(
        gate_id="L2b",
        pattern=r"""["']skills["']|skills:""",
        scope_dirs=("src/modex_agent/multi_agent/pool_config",),
        suffixes=(".py",),
        wave="W1.4",
    ),
    Gate(
        gate_id="L2c",
        pattern=r"\.skills\b|SKILL_SOURCE|\bskills\??\s*:",
        scope_dirs=(),
        scope_files=("examples/bot_project/webui/src/types/pool.ts",),
        suffixes=(".ts",),
        wave="W1.4",
    ),
    # --- L3: MEMORY_SYSTEM_MODIFIER + defaults/memory.py deletion (W1.1/W1.2) ---
    Gate(
        gate_id="L3a",
        pattern=(r"MEMORY_SYSTEM_MODIFIER|register_memory_system_modifier|memory_system_modifier"),
        scope_dirs=("src", "examples", "tests"),
        suffixes=(".py",),
        wave="W1.2",
    ),
    Gate(
        gate_id="L3b",
        pattern=r"plugins\.defaults\.memory",
        scope_dirs=("src", "examples", "tests", "docs"),
        suffixes=(".py", ".md"),
        wave="W1.1 (docs/ scope at W7.3)",
    ),
    Gate(
        gate_id="L3c",
        pattern=r"register_default_memory",
        scope_dirs=("src", "tests"),
        suffixes=(".py",),
        wave="W1.2",
    ),
    # --- L4: triggers.py dead registration (W1.4) ---
    Gate(
        gate_id="L4",
        pattern=r"TriggerFactory|register_default_triggers|defaults\.triggers",
        scope_dirs=("src", "tests"),
        suffixes=(".py",),
        wave="W1.4",
    ),
    # --- L5: MemoryOverrides.max_messages dead config (W5.1) ---
    Gate(
        gate_id="L5",
        pattern=r"\bmax_messages\b",
        scope_dirs=("src/modex_agent/plugins",),
        suffixes=(".py",),
        wave="W5.1",
    ),
    # --- L6: multi-provider model.yml bridge (W4.3) ---
    Gate(
        gate_id="L6",
        pattern=r"_llm_config_from_multi_provider|multi_provider",
        scope_dirs=("src/modex_agent/plugins", "tests"),
        suffixes=(".py",),
        wave="W4.3",
    ),
    # --- C1: single LLM provider resolution path (W4.2) ---
    Gate(
        gate_id="C1a",
        pattern=r"strategy_result\.provider",
        scope_dirs=("examples",),
        suffixes=(".py",),
        wave="W4.2",
    ),
    Gate(
        gate_id="C1b",
        pattern=r"BotModelProvider\(",
        scope_dirs=("examples/bot_project/bot", "examples/bot_project/plugins"),
        suffixes=(".py",),
        wave="W4.2",
        # Allowlist (learnings.md GATE SPEC ERRATA; amendable data):
        # - model_provider.py: the class definition.
        # - bot_strategies.py: the bot_default factory (the C1 single
        #   construction path for pool assembly).
        # - core.py: _build_default_provider — the independent-legitimate
        #   bot-global provider for the memory summarizer / experience review
        #   (W0.4 verdict, plan §2.C1; its None-when-no-model.yml behavior is
        #   deliberate). Added at W4.2 when builders.py:287 died.
        # - eval/harbor/pool_mode.py: the eval trial's bot_default factory —
        #   the harbor pool entry builds a private ComponentRegistry (the
        #   eval process has no production service singleton) and registers
        #   the provider that the pool-budget decorator then wraps; the
        #   eval-side mirror of bot_strategies.py's role, from the trial's
        #   EntryConfig-derived model config.
        # F2 hardening: the scope includes examples/bot_project/plugins so the
        # gate scans its own allowlisted factory file; expected_allowed_hits=4
        # pins the four justified sites - a FIFTH construction anywhere
        # under the bot project (bot/ or plugins/) fails.
        allowed_files=(
            "examples/bot_project/bot/service/model_provider.py",
            "examples/bot_project/plugins/bot_strategies.py",
            "examples/bot_project/bot/service/core.py",
            "examples/bot_project/bot/eval/harbor/pool_mode.py",
        ),
        expected_allowed_hits=4,
    ),
    # --- C2: hooks +/- incremental merge converges into SpecBuilder (W5.2) ---
    Gate(
        gate_id="C2",
        pattern=r'removeprefix\("\+"\)',
        scope_dirs=("src", "examples"),
        suffixes=(".py",),
        wave="W5.2",
        allowed_files=("src/modex_agent/scope/derivation.py",),
        expected_allowed_hits=1,
    ),
    # --- G-CAP1: CAPABILITY is the ONLY compile-time-resolved slot (W4, ---
    # --- capability-bundles todo 20 / SPEC §15 OQ5)                      ---
    Gate(
        gate_id="G-CAP1",
        pattern=r"registry\.resolve\(|ComponentSlot\.(?!CAPABILITY\b)\w+",
        scope_dirs=("src/modex_agent/scope",),
        suffixes=(".py",),
        wave="W4 (capability-bundles todo 20)",
        # Zero-hit gate by design: inside the scope package the ONLY
        # sanctioned registry faces are `resolve_capability(name)` (the
        # typed CAPABILITY accessor — every other slot is late-binding at
        # assembly time) and `registry.names(ComponentSlot.CAPABILITY)`
        # (the C0 enumeration, excluded by the lookahead). The two
        # alternatives catch:
        #   * `registry.resolve(` — ANY direct resolve call, including
        #     variable-slot indirection (`registry.resolve(slot, name)`);
        #   * `ComponentSlot.<OTHER>` — any non-CAPABILITY member access,
        #     including enumeration (`names(ComponentSlot.HOOK)`) and
        #     docstring/comment mentions (which must stay clean too).
    ),
    # --- G-CAP2: assembly-path unconditional component injection is dead ---
    # --- (W4, capability-bundles todo 20 / SPEC §13 W6 + §14.8; the       ---
    # --- allowlist SHRANK with todo 23's W6 glue eradication)             ---
    Gate(
        gate_id="G-CAP2",
        pattern=r"hook_runner\.add\(|\.add_cleanup_hook\(|register_tree_aware_hooks\(",
        scope_dirs=("src/modex_agent/plugins/assembly", "examples/bot_project/bot/service"),
        scope_files=("src/modex_agent/multi_agent/template.py",),
        suffixes=(".py",),
        wave="W4 (capability-bundles todo 20)",
        # Allowlist (each site is roster-driven dispatch or audited
        # deployment glue; the T23 W6 sweep already shrank this list —
        # the retired tree-aware wiring function's two calls and the
        # template.py entry died with it):
        # - native_core.py: the THREE sanctioned dispatch/wiring sites —
        #   `_dispatch_hooks`'s react branch (`hook_runner.add` with the
        #   factory priority), its memory branch (`add_cleanup_hook`),
        #   and the extra_hooks dedup loop (roster-name-gated re-add).
        # - pipeline_wiring.py: the `_add_hook` helper — the two
        #   remaining deployment-level outcome hooks (TurnOutcomeNotify /
        #   CassetteFlush); deliver_retry / length_guard / native_env /
        #   model_choice_bind all ride the compiler roster since T23.
        # - external_strategy.py: the external-sub auto-send dispatch —
        #   resolves the registered HOOK-slot factory (roster-adjacent:
        #   external subs never run the native capability dispatch), the
        #   T16-converged single construction home for that hook.
        # template.py: ZERO hits since T23 (its tree-aware wiring call
        # and native_env construction died with the position-default
        # roster rows) — the file stays in scope_files so a
        # reintroduction fails the gate.
        # Eval harnesses (bot/eval/) are OUT of scope by design: the gate
        # guards the production assembly path, not test instrumentation.
        allowed_files=(
            "src/modex_agent/plugins/assembly/native_core.py",
            "examples/bot_project/bot/service/pool/pipeline_wiring.py",
            "examples/bot_project/bot/service/external_strategy.py",
        ),
        expected_allowed_hits=5,
    ),
)


def _iter_files(root: Path, suffixes: frozenset[str]) -> Iterator[Path]:
    """Yield files under root matching suffixes; prune excluded dir names.

    Symlinked directories are skipped (grep -r semantics); entries are sorted
    so the walk order is deterministic.
    """
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.is_dir() and not entry.is_symlink():
            if entry.name in EXCLUDED_DIR_NAMES:
                continue
            yield from _iter_files(entry, suffixes)
        elif entry.is_file() and entry.suffix.lower() in suffixes:
            yield entry


def _scan_file(path: Path, rel_posix: str, pattern: re.Pattern[str]) -> list[GateHit] | None:
    """Return per-line hits of pattern in path (utf-8, lossy on bad bytes).

    ``None`` (not ``[]``) signals an unreadable file: the caller records a
    scope error so the gate FAILS closed - an unscanned file cannot prove
    absence of hits, matching the missing-scope discipline.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # Loud, never silent: an unreadable file is a potential under-match.
        print(f"[warn] unreadable file skipped: {rel_posix} ({exc})", file=sys.stderr)
        return None
    return [
        GateHit(rel_path=rel_posix, line_no=line_no, line_text=line)
        for line_no, line in enumerate(text.splitlines(), start=1)
        if pattern.search(line)
    ]


def _collect_gate(gate: Gate) -> GateReport:
    """Scan every scope of gate and evaluate its pass rule."""
    pattern = re.compile(gate.pattern)
    suffixes = frozenset(suffix.lower() for suffix in gate.suffixes)
    hits: list[GateHit] = []
    scope_errors: list[str] = []
    seen: set[str] = set()

    def _scan(path: Path) -> None:
        rel_posix = path.relative_to(REPO_ROOT).as_posix()
        if rel_posix in seen:  # scopes may overlap; count each file once
            return
        seen.add(rel_posix)
        file_hits = _scan_file(path, rel_posix, pattern)
        if file_hits is None:
            # Fail-closed: an unreadable file is an unscanned scope - the
            # gate cannot prove completeness, so it must FAIL (same
            # discipline as a missing scope dir/file).
            scope_errors.append(f"unreadable file: {rel_posix}")
            return
        hits.extend(file_hits)

    for rel_dir in gate.scope_dirs:
        root = REPO_ROOT / Path(rel_dir)
        if not root.is_dir():
            scope_errors.append(f"scope dir missing: {rel_dir}")
            continue
        for file_path in _iter_files(root, suffixes):
            _scan(file_path)
    for rel_file in gate.scope_files:
        file_path = REPO_ROOT / Path(rel_file)
        if not file_path.is_file():
            scope_errors.append(f"scope file missing: {rel_file}")
            continue
        _scan(file_path)

    allowed = set(gate.allowed_files)
    disallowed = tuple(hit for hit in hits if hit.rel_path not in allowed)
    allowed_count = len(hits) - len(disallowed)
    if gate.allowed_files:
        passed = not disallowed and (
            gate.expected_allowed_hits is None or allowed_count == gate.expected_allowed_hits
        )
    else:
        passed = not hits
    if scope_errors:
        # A missing scope cannot prove completeness - force FAIL, loudly.
        passed = False
    return GateReport(
        gate=gate,
        hits=tuple(hits),
        disallowed=disallowed,
        passed=passed,
        scope_errors=tuple(scope_errors),
    )


def _display_path(rel_posix: str) -> str:
    """Render a repo-relative posix path in OS-native form."""
    return str(Path(rel_posix))


def _trim(line: str) -> str:
    """Strip a source line and truncate it for compact sample output."""
    text = line.strip()
    if len(text) > MAX_SAMPLE_LINE:
        return text[: MAX_SAMPLE_LINE - 3] + "..."
    return text


def _print_report(reports: Sequence[GateReport]) -> None:
    """Print per-gate status, samples, and the X/N summary line."""
    for report in reports:
        gate = report.gate
        status = "PASS" if report.passed else "FAIL"
        detail = f"hits={len(report.hits)}"
        if gate.allowed_files:
            detail += f" disallowed={len(report.disallowed)}"
        print(f"{gate.gate_id:<4} {status}  {detail}  (green at {gate.wave})")
        if gate.allowed_files:
            rule = ", ".join(gate.allowed_files)
            if gate.expected_allowed_hits is not None:
                rule += f" (exactly {gate.expected_allowed_hits} hit required)"
            print(f"  allowed: {rule}")
        allowed = set(gate.allowed_files)
        samples = list(report.disallowed[:MAX_SAMPLES])
        if len(samples) < MAX_SAMPLES:
            allowed_hits = [hit for hit in report.hits if hit.rel_path in allowed]
            samples.extend(allowed_hits[: MAX_SAMPLES - len(samples)])
        for hit in samples:
            marker = "  [allowed]" if hit.rel_path in allowed else ""
            print(f"  {_display_path(hit.rel_path)}:{hit.line_no}: {_trim(hit.line_text)}{marker}")
        for error in report.scope_errors:
            print(f"  [scope error] {error}")
    green = sum(1 for report in reports if report.passed)
    print(f"\nSummary: {green}/{len(reports)} gates green")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the slot-rationalization removal/convergence gates "
            "(.omo/plans/slot-rationalization-steps.md sections 1-2)."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--baseline",
        action="store_true",
        help="report mode: print gate status, always exit 0 (default)",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="exit 0 iff all selected gates pass, else exit 1",
    )
    parser.add_argument(
        "--gate",
        action="append",
        default=None,
        help="gate id filter; repeatable and/or comma-separated (e.g. --gate L1a,L1b)",
    )
    return parser.parse_args(argv)


def _select_gates(filter_values: Sequence[str] | None) -> tuple[Gate, ...]:
    """Resolve --gate filter values to gates; exit 2 on unknown/empty input."""
    by_id = {gate.gate_id: gate for gate in GATES}
    if not filter_values:
        return GATES
    wanted: list[str] = []
    for value in filter_values:
        for gate_id in value.split(","):
            gate_id = gate_id.strip()
            if gate_id and gate_id not in wanted:
                wanted.append(gate_id)
    if not wanted:
        print("error: --gate given but no gate ids parsed", file=sys.stderr)
        raise SystemExit(2)
    unknown = [gate_id for gate_id in wanted if gate_id not in by_id]
    if unknown:
        print(
            f"error: unknown gate id(s): {', '.join(unknown)}"
            f"; valid ids: {', '.join(sorted(by_id))}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return tuple(by_id[gate_id] for gate_id in wanted)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    selected = _select_gates(args.gate)
    mode = "check" if args.check else "baseline"
    print(f"slot-rationalization gates - mode: {mode}")
    print(f"repo root: {REPO_ROOT}")
    print()
    reports = [_collect_gate(gate) for gate in selected]
    _print_report(reports)
    if args.check:
        return 0 if all(report.passed for report in reports) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
