# Self-Improve Runtime Mechanism

> Ensures the system can recover and continue working after context explosion, crash, or compression
> Core principles: **Progressive advancement, handover record, high-value revisit, multiple output channels**

---

## I. Progressive Advancement Flow

### Flow Design

```
┌─────────────────────────────────────────────────────────┐
│ Step 1: Scan corpus → Extract signals → Write to feedback│
├─────────────────────────────────────────────────────────┤
│ Step 2: Distill and classify → Three-level distillation → Write to themes│
├─────────────────────────────────────────────────────────┤
│ Step 3: Memory elevation → Manage hot.md                │
├─────────────────────────────────────────────────────────┤
│ Step 4: Determine output → Multi-channel routing        │
├─────────────────────────────────────────────────────────┤
│ Step 5: Self-reflection → Write reflections.md          │
├─────────────────────────────────────────────────────────┤
│ Step 6: Team profile → Update profile.md (every 3 times)│
├─────────────────────────────────────────────────────────┤
│ Step 7: Notification → Notify user                      │
└─────────────────────────────────────────────────────────┘
```

### Context Control Principles

| Principle | Description |
|-----------|-------------|
| Only load previous step's output | Don't look back to read original corpus (unless high-value revisit) |
| Index first | Read data_structure.md first, load on demand |
| Write checkpoint after each step | Record progress, can resume at any time |

---

## II. Handover Record Mechanism

### checkpoint.json Structure

```json
{
  "run_id": "YYYY-MM-DDTHH:MM:SS+TZ",
  "current_step": "classifier",
  "status": "in_progress",
  "completed_steps": [
    {"step": "scan", "status": "success", "output": "data/feedback/YYYY-MM-DD.jsonl", "count": 4},
    {"step": "evaluate", "status": "success", "output": null, "count": 4},
    {"step": "classify", "status": "in_progress", "output": null}
  ],
  "pending_steps": ["memory-layer", "output", "revisit"],
  "high_value_items": [
    {"source": "feedback#3", "reason": "Can distill into blog: system design philosophy", "potential": ["blog", "methodology"]}
  ],
  "last_update": "YYYY-MM-DDTHH:MM:SS+TZ"
}
```

### After Each Step Completes

1. Update checkpoint.json
2. Write progress record to run-log.jsonl

---

## III. Cold Start Recovery Flow

### Recovery Steps

```
1. Read checkpoint.json
   - Has incomplete run? → Continue executing pending_steps
   - None? → Start new run

2. Read previous step's output file
   - Don't need to start from scratch
   - Only load necessary data

3. Continue execution
   - Start from current_step
   - Don't repeat completed work

4. Cleanup after completion
   - Mark status: "completed"
   - Update last_success_ts in config.yaml
```

### Context Size Estimation

| Stage | Content Loaded | Size |
|-------|---------------|------|
| Recovery state | checkpoint.json | ~1KB |
| Step input | Previous step output file | ~5-20KB |
| Current execution | Processing data | Dynamic |

**Total: Each recovery only needs ~10-30KB, won't explode**

---

## IV. High-Value Revisit Mechanism

### Identifying High-Value

**High-value judgment and marking occurs in Step 5: Determine output.**

The proposer module holistically considers content in hot.md and themes/, marking `high_value` based on the following criteria:
- **High repetition count**: Rules in hot.md with many occurrences
- **Wide impact scope**: Applicable to multiple agents or system-level
- **High distillability**: Can be distilled into universal methodologies or blog posts
- **High value density**: Large information content, condensed through multiple rounds of conversation

And writes items marked as `high_value` to `data/high-value/`.

### Revisit Flow

```
After Step 5 completes:
  Check high_value_items
    ↓
  Has high-value items?
    ↓
  Step 6: Revisit
    - Deep read of original corpus (only now look back)
    - Deep distillation
    - Write to corresponding output channel
```

**Revisit is optional**, only executed when high-value is discovered.

---

## V. Multiple Output Channels

### Output Form Determination

The proposer determines which channel material should output to:

| Output Form | Target Location | Determination Basis |
|------------|-----------------|---------------------|
| Rule solidification | PENDING.md | Behavior norms, appears ≥3 times |
| Skill improvement | skill improvement suggestions | Skill usage issues |
| Blog post | drafts/blog-xxx.md | Has universal value, shareable |
| Methodology | /path/to/learned/methodologies/ | Reusable thinking frameworks |
| Knowledge point | data/errors/ or data/lessons/ | Specific knowledge points |
| Business insight | /path/to/learned/business/ | Business related |
| System improvement | PENDING.md (system-upgrade) | System design issues |

### Blog Output Flow

```
1. proposer determines "can become article"
2. Write to drafts/blog-{topic}.md (draft)
3. Can be polished by agent later
4. Publish to blog platform
```

### Draft Format

```markdown
# {Title}

> Draft status: Pending polish
> Source: self-improve run {run_id}
> Output time: {time}

## Core Points

{Distilled core points}

## Body

{Article body}

## Items Pending Polish

- [ ] Title optimization
- [ ] Opening appeal
- [ ] Ending elevation
- [ ] Case supplementation
```

---

## VI. Data Flow Overview

```
Original corpus (memory logs)
    ↓ Scan extract
feedback/*.jsonl
    ↓ Evaluate
Evaluation results
    ↓ Classify
themes/{theme}/
    ↓ Elevation/demotion
hot.md
    ↓
    ↓ Step 5: Holistic consideration (themes/ (main) + hot.md (rule solidification specific) + errors/ + lessons/)
    ↓
┌────────────┬────────────┬────────────┬────────────┐
↓            ↓            ↓            ↓            ↓
PENDING.md   methods/     drafts/      errors/      lessons/
(Rule solidification)(Methodology) (Blog)       (Errors)      (Lessons)
                                       ↓            ↓
                                       └────────────┘
                                            ↓
                                    Next round proposer revisit check
                                    Discover commonalities → Upgrade to rules/methodologies
```

**Closed loop: errors/ and lessons/ are both output locations and input sources for the next round.**

---

## VII. File Structure Updates

```
/path/to/self-improve/
├── checkpoint.json          # Handover record (new)
├── run-log.jsonl            # Progress log
├── config.yaml              # Configuration (contains last_success_ts)
│
├── data/
│   ├── feedback/            # Step 1 output
│   ├── themes/              # Step 3 output
│   ├── hot.md               # Step 4 output
│   ├── errors/              # Error knowledge points (intermediate station, next round revisit)
│   ├── lessons/             # Experience lessons (intermediate station, next round revisit)
│   └── high-value/          # High-value item records
│
├── proposals/
│   └── PENDING.md           # Rule solidification proposals
│
└── drafts/                  # Blog drafts
    └── blog-xxx.md

/path/to/learned/                  # Independent output directory (not inside self-improve)
├── methodologies/
├── business/
├── innovations/
└── articles/
```

---

## VIII. Relationship with Existing Mechanisms

| Existing Mechanism | New Mechanism | Relationship |
|-------------------|---------------|--------------|
| run-log.jsonl | checkpoint.json | run-log records history, checkpoint records current state |
| cron-trigger.md | Progressive advancement flow | Transform cron-trigger to execute step by step |
| feedback-collector | Step 1 | Unchanged, but must write checkpoint after completion |
| proposer | Step 5 | Expand output judgment capability |
| /path/to/learned/ | One output channel | Methodologies, business insights, innovations |
