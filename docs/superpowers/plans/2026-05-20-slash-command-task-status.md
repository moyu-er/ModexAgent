# Slash Command Handling Task Status

This file tracks implementation progress for:

- Plan: `docs/superpowers/plans/2026-05-20-slash-command-implementation.md`
- Spec: `docs/superpowers/specs/2026-05-20-slash-command-design.md`

## Required Tracking Rule

Every time an implementation task from the plan is completed, update this file
in the same working session:

1. Change the task checkbox from unchecked to checked.
2. Add a short note under the task explaining what was completed.
3. If verification was run for that task, include the exact command and result.
4. Do not mark future tasks complete early.

This tracking rule is part of the implementation requirements. The task is not
considered complete until this file records it.

## Tasks

- [x] Task 1: Add Command Type Model
  - Note: Added `framework.commands` constants, dataclasses, and public exports.
  - Verification: `python -c "from framework.commands import CommandAction, CommandContext, SlashCommandInvocation; print(CommandAction.NOTICE.value)"` passed and printed `notice`.

- [x] Task 2: Implement Strict Slash Parser
  - Note: Added pure slash-command parser and parser unit tests for plain, valid, and invalid input.
  - Verification: `python -m pytest tests/unit/commands/test_parser.py -v` passed with 14 tests.

- [x] Task 3: Implement Default Command Handlers
  - Note: Added approval, continue, unknown, and invalid command handlers with typed handling results.
  - Verification: `python -m pytest tests/unit/commands/test_handlers.py -v` passed with 6 tests.

- [ ] Task 4: Add Skill Handler and Processor
  - Note: Not started.

- [ ] Task 5: Make Context Assembly Accept Turn Requests Without User Input
  - Note: Not started.

- [ ] Task 6: Integrate Command Processor Into AgentPipeline
  - Note: Not started.

- [ ] Task 7: Migrate Approval Parsing to Slash Commands
  - Note: Not started.

- [ ] Task 8: Add Pre-Lock Dispatch Policy Hook Point
  - Note: Not started.

- [ ] Task 9: Wire bot_project Main Pipeline
  - Note: Not started.

- [ ] Task 10: Full Verification and Documentation Cleanup
  - Note: Not started.
