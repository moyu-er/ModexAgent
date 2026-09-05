<!-- Parent: ../../../AGENTS.md -->
<!-- Updated: 2026-09-04 | strict parsing, visibility, roots, and tail injection -->

# skills

## Purpose

The bundled Skills capability owns skill loading, filtering, caching, prompt
rendering, command resolution, and per-pool catalog supply. Generic consumers
depend on `modex_agent.commands.skill.SkillResolver`; the concrete
`SkillCatalog` stays inside this package.

## Key Files

| File | Description |
|------|-------------|
| `capability.py` | `SkillsCapability`: native auto-apply, prompt-section contribution, pool supply, agent wiring, and `require_skills_supply`. |
| `supply.py` | `SkillsSupply`: the per-pool `agent_name -> SkillCatalog` owner. `resolver_for(agent_name)` is the only bound-resolver factory. |
| `catalog.py` | `SkillCatalog`: concrete `SkillResolver` coordinating source, filter, cache, prompt rendering, command XML, and resources. |
| `source.py` | `SkillSource` ABC with File, Inline, and Composite adapters. |
| `models.py` | Frozen skill metadata, resource, summary, document, and resolution-context values. |
| `cache.py` / `filter.py` / `builder.py` | Catalog collaborators for disk freshness, visibility, prompt indexes, and canonical command XML. |
| `section.py` | `SkillSectionProvider` for the `skills.injection` capability section. |
| `registration.py` | `register_skills_feature`: the package's CAPABILITY-slot registration entry. |
| `modex_agent/commands/skill.py` | Consumer-owned `SkillResolver` and `ResolvedSkillCommand` contract. |

## Invariants

- `SkillsCapability` auto-applies to every native agent. An explicit
  `capabilities: {skills: false}` veto removes prompt and command resolution for
  that agent. External agents are structurally excluded before capability
  predicates run.
- `SkillsSupply` is built once per pool and owns exactly one `SkillCatalog` per
  effective agent. Main and subagent assembly perform lookup only; missing
  assignment directories yield an empty catalog, not missing wiring.
- Disk is the assignment authority: the conventional root is
  `skills/<pool>/<agent>/`; `capabilities: {skills: {roots: [...]}}` prepends
  per-agent roots, and the conventional root wins duplicate names. Missing
  roots stay watched so later directory creation is detected.
- `SKILL.md` frontmatter is parsed once into metadata. `Skill.content` is always
  body-only; canonical slash XML must never contain frontmatter.
- `disable-model-invocation: true` filters only the `TAIL` system-prompt view.
  The complete catalog remains available to explicit `/name` invocation; an
  all-hidden catalog emits no Skills section.
- The two command onramps share one resolver contract and canonical XML path:
  Bot `SkillParseStage` uses the root resolver exposed by
  `PoolInstance.skill_resolver`; framework `SkillCommandHandler` uses the bound
  resolver in `CommandContext`. Subagents retain their own bound resolver.
- Full design and acceptance matrix:
  `docs/design/skills/skill-system.md`.

## Testing

- `tests/unit/plugins/skills/`: package and supply behavior.
- `tests/unit/pipeline/test_pipeline_skills.py`: direct command onramp.
- `examples/bot_project/tests/input_pipeline/test_skill_parse.py`: Bot onramp.
- `tests/unit/scope/`: native auto-apply, explicit veto, and external exclusion.
