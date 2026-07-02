"""ADR-0015 D5: _fork_workspace / _resolve_output_root deleted from
AgentCommunicationService; behavior relocated to WorkspacePathResolver.

The four tests that lived here asserted the deleted methods never escaped to
project_dir or the process CWD when workspace pool_data was unresolved. That
safety property is now covered by WorkspacePathResolver's contract — see
test_workspace_paths.py:
  - test_runtime_dir_returns_none_when_no_workspace
  - test_runtime_dir_returns_none_when_workspace_unmaterialized
  - test_memory_dir_prefers_workspace
  - test_runtime_dir_prefers_workspace_pool_data
"""
