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

- [x] Task 4: Add Skill Handler and Processor
  - Note: Added dynamic skill command handling and `SlashCommandProcessor` with built-in command priority.
  - Verification: `python -m pytest tests/unit/commands/ -v` passed with 26 tests.

- [x] Task 5: Make Context Assembly Accept Turn Requests Without User Input
  - Note: Extended context assembly with `append_user_message` so `/continue` can trigger a turn without adding user text.
  - Verification: `python -m pytest tests/unit/pipeline/test_slash_commands.py::test_assemble_context_can_skip_user_append_for_continue tests/unit/pipeline/test_slash_commands.py::test_assemble_context_appends_transformed_skill_content -v` passed with 2 tests.

- [x] Task 6: Integrate Command Processor Into AgentPipeline
  - Note: Added `TurnRequest`, optional `command_processor`, and pipeline routing for continue and transformed skill inputs.
  - Verification: `python -m pytest tests/unit/pipeline/test_slash_commands.py -v` passed with 4 tests.

- [x] Task 7: Migrate Approval Parsing to Slash Commands
  - Note: Replaced approval command parsing with slash-only parser delegation and updated approval tests.
  - Verification: `python -m pytest tests/unit/approval/test_detect_approval_command.py tests/unit/pipeline/test_approval_renderer_edge.py -v` passed with 15 tests.

- [x] Task 8: Add Pre-Lock Dispatch Policy Hook Point
  - Note: Added pre-lock dispatch policy lookup in `AgentPipeline` and reserved a no-op bypass path for future immediate commands.
  - Verification: `python -m pytest tests/unit/pipeline/test_slash_commands.py -v` passed with 5 tests.

- [x] Task 9: Wire bot_project Main Pipeline
  - Note: Added bot_project main command processor builder and injected it into main pipeline and main pool pipeline only.
  - Verification: `python -m pytest examples/bot_project/tests/test_slash_commands.py -v` passed with 1 test.

- [ ] Task 10: Full Verification and Documentation Cleanup
  - Note: Not started.
