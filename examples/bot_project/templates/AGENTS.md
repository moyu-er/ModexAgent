<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-13 -->

# templates

Template files used during agent memory initialization. These Markdown files serve as seed content for the agent's knowledge and personality layers.

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `knowledge/` | Knowledge seed templates |

## knowledge/

| File | Description |
|------|-------------|
| `MEMORY.md` | Template for agent memory structure — used as seed for long-term memory |
| `SOUL.md` | Template for agent personality and behavioral guidelines |
| `USER.md` | Template for user profile tracking and preferences |

## For AI Agents

### Working In This Directory
- These templates are read during agent initialization to seed the knowledge layer.
- Do not add runtime logic here — these are pure content templates.
- Content follows Markdown format with frontmatter-style headers.
