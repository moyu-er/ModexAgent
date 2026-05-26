# Self-Improve System Documentation

> Version: 2.2.0
> This is the team's self-evolution operating system. Any agent reading this file knows everything.

---

## What This Is

A pluggable, modular self-improvement framework. Agents learn from errors, corrections, and feedback, accumulate reusable experience rules, and continuously improve execution quality. Runs automatically on schedule (every 3 days), only requiring user confirmation when solidifying into system files.

---

## Core Principles

1. **System file modifications require approval** — See `approval_required` list in config.yaml
2. **Evidence first** — No inference from silence, 3 repetitions required for solidification
3. **Team-wide sharing** — What one agent learns benefits all agents
4. **Infinitely extensible** — Add module = add directory + write MODULE.md + register
5. **Scheduled trigger** — Runs automatically every 3 days, not triggered in conversations
6. **Fully automatic processing** — Collection, classification, and distillation are fully automatic
7. **Self-determined format** — The language model decides how to organize language, which section to add to, or whether to create a new section
8. **Fault-tolerant continuation** — When a step fails, log the error and continue with subsequent steps without interrupting the entire process

### Runtime Principles

9. **Progressive advancement** — Each step only loads the previous step's output, no looking back
10. **Handover record** — Write checkpoint after each step completes, can resume at any time
11. **Context control** — Release corpus after extraction, don't keep it occupied
12. **High-value revisit** — Mark when high-value is discovered, for later deep reading and distillation
13. **Multiple output channels** — Rule solidification, skill improvement, blog posts, methodologies, etc.

---

## Directory Structure

```
{installation_directory}/
│
├── ENGINE.md              # Power mechanism (trigger rules, dependencies, classification mapping)
├── SYSTEM.md              # This file
├── RUNTIME.md             # Runtime mechanism (progressive advancement, handover record, recovery flow)
├── config.yaml            # Module registry + switches + approval rules
├── user-config.yaml       # User configuration (paths, timezone, etc.)
├── checkpoint.json        # Handover record (current running state)
├── changelog.md           # System upgrade log
│
├── modules/               # Pluggable modules (each has MODULE.md)
│   ├── feedback-collector/   # Collect feedback
│   ├── distill-classifier/   # Distill + Classify
│   ├── memory-layer/         # Layered memory management
│   ├── proposer/             # Solidification proposals
│   ├── profiler/             # Capability profile
│   ├── reflector/            # Self-reflection
│   └── notify/               # Notification
│
├── data/                  # Shared data for all modules
│   ├── hot.md                # HOT Layer: ≤100 lines of active rules
│   ├── high-value/           # High-value item records
│   ├── corrections.md        # Correction log (most recent 50 entries)
│   ├── reflections.md        # Self-reflection log
│   ├── profile.md            # Team capability profile
│   │
│   ├── feedback/             # Structured feedback YYYY-MM-DD.jsonl
│   ├── themes/               # Theme classification
│   │   ├── behavior/         # Behavior norms
│   │   ├── communication/    # Communication preferences
│   │   ├── tools/            # Tool usage
│   │   └── ...               # Other themes
│   │
│   ├── backup/               # Pre-run backup
│   └── archive/              # Cold storage
│
├── proposals/             # Approval queue
│   └── PENDING.md            # Pending modification proposals
│
├── drafts/                # Blog drafts
│
└── scripts/               # CLI tools
    ├── setup.mjs             # One-click installation
    ├── run-all.mjs           # Execute all modules
    ├── module.mjs            # Module management
    └── ...
```

---

## How to Use

### Daily Usage (Fully Automatic)

Every 3 days at 4 AM (configurable), Cron automatically triggers the core process:

```
0. Backup               → Copy critical files to data/backup/
1. Scan corpus          → feedback-collector extracts signals from memory logs
2. Distill and classify → distill-classifier performs three-level distillation + classification
3. Memory elevation     → memory-layer updates hot.md
4. Determine output     → proposer decides destination
5. High-value revisit (optional) → Deep read of original corpus, deep distillation
6. Wrap-up              → Self-reflection, team profile, notification
```

**The authoritative process is defined in `prompts/cron-trigger.md`.**

### During Daily Work (Passive Recognition)

Agents recognize learning signals during normal work and record them in data/:

| Event | Action |
|-------|--------|
| User corrected you | Record to corrections.md |
| User praised you | Record positive feedback |
| Completed important Task | Write self-reflection |
| Discovered reusable Rule | Record to corrections.md |
| Rule involves system files | Write to PENDING.md |

### Active Query

| Query | Action |
|-------|--------|
| "What did you learn" | Show recent corrections + reflections |
| "Check improvement proposals" | Show proposals/PENDING.md |
| "Improvement statistics" | Run scripts/status.mjs |
| "Forget X" | Delete from all layers (after confirmation) |

### Manual Trigger (Optional)

```bash
# Execute all modules
node scripts/run-all.mjs

# Execute a single module
node scripts/feedback.mjs log
node scripts/status.mjs
```

---

## Module System

### Module Standard

Each module is a directory under `modules/` and must contain `MODULE.md`.

### Core Modules

| Module | Responsibility | Output |
|--------|---------------|--------|
| feedback-collector | Scan conversations, extract signals | data/feedback/*.jsonl |
| distill-classifier | Three-level distillation + classification | data/themes/ |
| memory-layer | Layered memory management | data/hot.md |
| proposer | Determine output form | proposals/PENDING.md, drafts/ |
| reflector | Self-reflection | data/reflections.md |
| profiler | Team capability profile | data/profile.md |
| notify | Notify user | Via message tool |

### Adding New Modules

1. Create new directory under `modules/`
2. Write `MODULE.md`
3. Register in `config.yaml` under `modules`
4. Record in `changelog.md`

Or use the command:
```bash
node scripts/module.mjs add new-module-name
```

---

## Approval Mechanism

`config.yaml` defines an `approval_required` list. Files in this list require:

1. Write to `proposals/PENDING.md`
2. Notify user
3. Execute after user confirmation
4. Mark as completed

### Files Requiring Confirmation

AGENTS.md, TOOLS.md, MEMORY.md, SOUL.md, HEARTBEAT.md, IDENTITY.md, openclaw.json, SKILL.md

### Files Executed Automatically

- `data/*` — Data for improving the system itself
- `{knowledge_root}/*` — Automatically distilled knowledge

---

## Data Layers

| Layer | Location | Size Limit | Behavior |
|-------|----------|------------|----------|
| HOT | data/hot.md | ≤100 lines | Written after Cron distillation |
| WARM | data/themes/ | Each file ≤200 lines | Loaded on demand by context |
| COLD | data/archive/ | Unlimited | Loaded only on explicit query |

Elevation/demotion rules:
- Applied 3 times/7 days → Elevate to HOT
- Unused for 30 days → Demote to WARM
- Unused for 90 days → Archive to COLD
- Never automatically deleted

---

## Learning Signals

### Triggers Learning

| Signal | Confidence | Action |
|--------|------------|--------|
| User explicitly corrects you | High | Immediately record to corrections.md |
| User repeats something | High | Mark as repeated, increase priority |
| User explicitly prefers something | Confirmed | Write directly to HOT |
| Same correction 3 times | Confirmed | Generate solidification proposal |
| Task failure/error | High | corrections.md + root cause analysis |
| User praises | Positive | Record success case +1 |

### Does Not Trigger Learning

- Silence
- Single instruction
- Hypothetical discussion
- Third-party preference
- Group chat mode (unless user confirms)

---

## Security Boundaries

### Never Store
Passwords, API Keys, Tokens, financial information, medical information, third-party personal information, location patterns

### Transparency
- "What did you remember" → Full export
- Each Rule is tagged with source and time
- "Forget X" → Delete from all layers

---

## Relationship with Existing Systems

```
memory/YYYY-MM-DD.md      → Fact records (what happened)
MEMORY.md                 → Long-term memory (important people, events, decisions)
{knowledge_root}/         → Automatically distilled knowledge
self-improve/             → Execution improvement (how to do better)
```

All four complement each other, with no overlap or substitution.

---

## Installation

```bash
# 1. Configure paths
cp user-config.yaml my-config.yaml
# Edit my-config.yaml

# 2. Run installation
node scripts/setup.mjs --config my-config.yaml

# 3. Check Cron suggestions
cat proposals/PENDING.md

# 4. Add Cron Task to OpenClaw configuration
```

---

## Onboarding New Agents

**Automated, no manual configuration required.**

During Cron execution, it automatically scans memory directories of all agents under `{workspace_root}`. Newly created agents are automatically scanned.

### Let New Agents Know the System Exists

Add to the new agent's AGENTS.md:

```markdown
## Self-Improvement

This team uses the self-improve system to continuously improve execution quality.

### During Daily Work
- When user corrects you → Record to corrections.md
- After completing important Tasks → Write self-reflection to reflections.md
- When discovering reusable Rules → Record to corrections.md
- When Rules involve system file modifications → Write to proposals/PENDING.md
```

---

## Not a Skill

self-improve is not an OpenClaw skill, and is not placed in `~/.openclaw/skills/`.

Reasons:
- Skills are for single agents, self-improve is shared by the entire team
- Skills are triggered by session startup matching, self-improve is triggered by Cron schedule
- self-improve is an independently installed framework
