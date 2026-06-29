# attachment-outbound Specification

## Purpose
TBD - created by archiving change add-attachment-system. Update Purpose after archive.
## Requirements
### Requirement: SendFileToUserTool emits a workspace-locator attachment

`SendFileToUserTool` SHALL produce an outbound Attachment with `locator=workspace`,
recording the literal absolute path the agent provided (resolved against the bound
workspace root at send time). The file SHALL remain in place; no copy SHALL be made
into the media directory.

#### Scenario: outbound file is referenced in place
- **WHEN** the agent sends a file at `/abs/path/report.pdf`
- **THEN** the Attachment record SHALL have `locator=workspace` and `path=/abs/path/report.pdf`, and the file SHALL NOT be copied

#### Scenario: outbound accepts an arbitrary absolute path
- **WHEN** the agent sends a file outside the workspace root at an absolute path
- **THEN** the Attachment SHALL record that path verbatim and download SHALL read it directly

### Requirement: Outbound is capped at a configurable 1 GB

A single outbound file larger than the 1 GB (configurable) cap SHALL be hard-rejected
at `SendFileToUserTool`. Outbound SHALL NOT pass the perception gate (the agent
produced the file deliberately).

#### Scenario: oversized outbound file is rejected
- **WHEN** the agent sends a 2 GB file
- **THEN** `SendFileToUserTool` SHALL reject it and inform the agent

#### Scenario: outbound bypasses the perception gate
- **WHEN** the agent sends a file whose type would fail the inbound perception gate
- **THEN** the outbound Attachment SHALL still be recorded (outbound is not gated by perception), subject only to the 1 GB cap

