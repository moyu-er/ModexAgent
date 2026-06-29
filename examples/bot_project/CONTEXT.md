# Bot Project Context

Example business wiring built on the ModexAgent framework (the `src/modex_agent/`
core): a QQ bot (botpy) with a webui, plugin system, skills, and message
templates.

## Input pipeline domain language

**Channel** — an origin a user message arrives from: IM (QQ) or WebUI. The two
channels run different stage subsets over the same envelope.

**Stage** — one ordered step in the input pipeline. A stage either *passes
through* (lets the envelope continue) or *claims* the input (handles it).

**Claim** — a stage recognising an input as its own responsibility and handling
it. A stage that cannot claim an input MUST pass it through; it never rejects on
behalf of a downstream stage. Claiming has two shapes:

- **Claim-and-terminate** — the stage fully resolves the input and stops the
  pipeline (e.g. an IM control command like `/cd`, `/stop`, `/pool`).
- **Claim-and-continue** — the stage resolves the input but lets the envelope
  continue so later stages (persistence, enqueue) still run (e.g. a skill, an
  approval decision). A claim-and-continue stage marks the envelope *resolved*.

**Resolved** — the state of an envelope whose slash command has been claimed.
Carried by `command_resolved`. It is the single signal the terminal stage reads;
no stage needs to know *which* other stage claimed the command.

**Terminal unsupported-command stage** — the last command-resolution stage,
sitting after every claiming stage but before persistence/enqueue. Its only
question is "is there still an unclaimed `/command`?" If a slash command reaches
it unresolved, it is by definition unsupported, and the stage returns the single
generic "command or skill not supported" notice. No other stage emits an
unsupported notice.

**Approval onramp** — the per-channel way an approval decision enters the
pipeline. WebUI builds the structured decision at its edge (the approvals POST);
IM's `ApprovalStage` interprets a typed `/approve` / `/deny`. Both converge on a
single structured `ApprovalDecisionInput` carried on the envelope, so the
downstream resume path is identical across channels.

**Decide-next-pending** (IM) vs **precision** (WebUI) — the two ways the resume
machine locates *which* request a decision applies to. IM carries no
`tool_call_id`, so the decision applies to the next still-pending request in
order; WebUI carries an explicit `tool_call_id` and targets exactly that one.
This is the only surviving approval divergence between channels — the input path
and the resume machine are otherwise one.
