<p align="center">
  <strong>English</strong> · <a href="workflows.zh-CN.md">简体中文</a> · <a href="../../README.md">Root README</a>
</p>

# Workflows

This guide follows user-visible tasks from workspace selection to a checked result. It uses the WebUI names exactly as they appear in the shipped reference application.

## Choose a Workspace, Pool, and Conversation

A workspace is the project boundary for files, runtime state, pools, and memory. A pool selects the root agent and the collaborators available to that conversation. A conversation keeps follow-up turns and any child-agent sessions together.

1. Use `Open workspace` in the workspace tab bar.
2. Select a recent folder, browse to one, or use `Create Workspace` for an isolated workspace.
3. In the left sidebar, choose an `Agent pool` when more than one pool is available.
4. Click `New Conversation`, then send the first message.

The selected pool is pinned when the new conversation is created. Changing the sidebar pool filters the conversation list; it does not move an existing conversation to another pool. Return to an existing conversation when the new task depends on its context, or start a new one for an independent task.

## Give the Agent a Closed Task

A useful request states the outcome, boundaries, verification, and expected report. The model may complete the task itself or use an available collaborator; delegation is not guaranteed.

### Coding task

Use a coding-capable pool and make the completion test explicit:

```text
Fix the failing CSV export when a field contains a newline.
First reproduce the bug, then make the smallest correct change.
Run the focused tests and report the changed files, test command, and result.
Do not change unrelated formatting or configuration.
```

Review the final answer for all four parts: diagnosis, change, verification, and remaining risk. If the result is incomplete, continue in the same conversation with the missing check rather than restating the whole task.

### Read-only research task

Use a pool with suitable read-only capabilities when the result must not change the workspace:

```text
Research how a WebUI message reaches the selected pool.
Do not edit files or run mutating commands.
Return the call path, key decisions, and exact file references.
List uncertainties separately from verified facts.
```

Prompt wording communicates intent but is not an enforcement boundary. If read-only behavior is mandatory, confirm the selected agent's configured toolset and sandbox policy. You may ask the root agent to use a read-only research collaborator if useful, but the model can reasonably choose another valid path.

## Follow the Work

### Session tree

The `Conversations` sidebar shows the root session and discovered child sessions as a tree. Expand a parent and select a child to inspect that child's transcript. Child sessions are shown as `Read only`, so continue the task from the root conversation.

A child appears only when the running agent actually creates one. If none appears, the root may have handled the task itself; absence alone does not mean the request failed. Newly discovered descendants are associated with their parent, including child sessions reported by OpenCode.

### Tool trace

Tool calls appear inline as collapsible cards named after the tool. Expand a card to inspect `Args` and `Result`; completed calls carry the `done` label. Use the trace to check what was read, changed, or executed rather than inferring actions from prose.

A successful tool result is not the same as a successful task. For coding work, also inspect the final summary and the reported test result. For research, check that claims are tied to evidence and that no write tool was used.

## Pause and Approvals

### Pause the current turn

While a conversation is responding, `Pause` cancels the current turn. It does not delete the conversation, so you can send a correction or narrower follow-up afterward. It also does not roll back commands or file writes that already completed; inspect the workspace before retrying.

Conversation `Pause` is different from Graph `Pause` below. The first ends one agent turn, while the second preserves a graph instance for `Resume`.

### Decide an approval request

Approval cards appear only when approval is enabled for a native agent and a tool call requires a decision. Read the tool name, severity, and arguments before choosing `Approve`.
Use `Approve All` only when every pending call is expected. `Deny All` rejects the batch; denying any one pending card cancels that whole batch.

Approval is configurable, not a promise that every tool call will pause. External OpenCode tools do not pass through the framework's native tool approval flow.

## Run a Graph Workflow

Graphs are workspace-scoped YAML specifications shown under `Graphs`. They coordinate nodes and persist an instance lifecycle; they are not the same thing as a chat conversation.

### Start in a disposable workspace

Agent nodes can edit files and run commands in the selected workspace. Before trying a coding graph, open a temporary clone, disposable copy, or dedicated Git worktree that includes the graph configuration.
Confirm the workspace path in the tab before starting, and review its diff afterward. This avoids testing `Pause`, `Deliver`, or a cyclic workflow against your real working tree.

Native agent turns inside a graph retain configured guards but do not open human tool-approval cards. Check the policy before running a workflow; do not rely on a chat approval setting to interrupt it for confirmation.

### Understand `review_cycle`

The shipped [`review_cycle.yml`](../../examples/bot_project/config/graphs/review_cycle.yml) connects `coder` to `reviewer`. The `coder` node implements the request and delivers a change summary to `reviewer`.
The `reviewer` is instructed to inspect the actual diff and verification results. It can deliver revision findings back to `coder`, or deliver the accepted result to the graph end.

This is a cycle, so give it a bounded, testable request and watch repeated node activity. The node-to-agent bridge is implemented in [`agent_node.py`](../../examples/bot_project/bot/graph/agent_node.py) for readers who want implementation detail.

### Create and inspect an instance

1. Open `Graphs` and select the `review_cycle` graph spec.
2. Click the plus button labeled `New Instance`.
3. Enter the initial request and click `Run`.
4. Open the instance row to see its status, progress, input, and output.
5. Click `Topology` to open the live topology view.
6. Use the sidebar section labeled `Events` as the event timeline.

Select a node to see its status, invocation, and result. For an agent node with a session, use `Open session transcript` to jump to its conversation trace.

### Control a running instance

`Pause` requests that a pending or running instance stop scheduling new work and settle into `paused`. Use `Resume` after the status is `paused` to continue the same instance.
Use `Stop` to end a stoppable instance when it should not continue; a stopped instance is not resumed.

When the instance is `running` or `paused`, `Deliver to node` is available. Choose a `Node`, enter content, and click `Deliver` to inject work into that node.
This is an operator control, not an ordinary chat reply, and can change which work becomes runnable.

### Relate graph instances to agent sessions

The graph instance owns topology, node state, events, input, and final output. Each agent-backed node runs through an agent session, where model output and tool traces are recorded.
A node session can itself have child sessions, which remain visible in the normal conversation tree. Graph `Pause` coordinates the active node session tree so the same graph instance can later resume.

### Edit the workflow

Open a graph spec and click `Edit YAML`. Edit the text in `YAML Configuration`, check the topology preview, then click `Save`.
Parse or validation errors are shown against the YAML and should be fixed before running another instance.

The topology is a preview, not a visual graph editor.
Dragging the canvas pans the view and scrolling zooms it; nodes and edges are changed in YAML, not by drag and drop.

## Use the Shipped OpenCode Integration

OpenCode is available as a shipped external coding-agent integration and as a selectable pool when its prerequisites are met.

### Prepare OpenCode

1. Install the `opencode` CLI separately and ensure it is on the `PATH` inherited by the ModexBot process.
2. Configure provider credentials and models using OpenCode's own configuration.
3. Verify the CLI can run against the intended workspace before selecting the `opencode` pool.
4. Restart the application after changing process-level `PATH` or pool declarations.

If the executable is unavailable at startup, the OpenCode pool is skipped and other pools remain usable.
Do not put provider secrets in workflow prompts or committed graph YAML.

### Know the ownership boundary

The WebUI is a shared workbench for choosing a workspace and session and for viewing streamed output.
For the `opencode` pool, provider-native sessions, generated output, model selection, tools, context, and permission behavior are owned by OpenCode.
ModexAgent maps the external session, routes its events into the shared transcript, and can display discovered child sessions.

Native MCP assignments, Skills, sandbox metadata, and approval settings must not be assumed to govern OpenCode's own tools.
Configure equivalent behavior in OpenCode itself and treat its permissions as the effective boundary.
See the [external coding-agent design](../design/external-coding-agent-integration/spec.md) for deeper integration details.

### Run and verify

Select the `opencode` pool before clicking `New Conversation`, then send the same closed-task format used above.
Follow text, reasoning, and tool cards in the shared transcript, and inspect child sessions if OpenCode creates them.
Pause if the current turn should stop, but verify the workspace because completed external tool actions are not rolled back.
Finish by checking the actual diff or artifact and the reported verification command.

## Use QQ or Telegram as an Access Channel

QQ and Telegram are additional ways to reach configured pools; they do not define a separate agent workflow.
Channel setup, credentials, and channel-specific controls belong in [Configuration](configuration.md).
Keep task closure the same: select the intended scope, state the result and checks, observe the run, and verify the artifact.

## Completion Checklist

- The active workspace and pinned pool are the intended ones.
- The final artifact or answer matches the request, not only the last tool result.
- Tool traces and child sessions support the claimed work.
- Required tests or read-only constraints were actually checked.
- Pending approvals are resolved, and no conversation or graph remains unintentionally active.
- Coding experiments ran in a disposable workspace and the resulting diff was reviewed.

---

[Getting Started](getting-started.md) · [Configuration](configuration.md) · [Extensions](extensions.md)
