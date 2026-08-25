"""ADR-0015 D5: _fork_workspace / _resolve_output_root deleted from
AgentCommunicationService; behavior relocated to the scope-path resolver.

The four tests that lived here asserted the deleted methods never escaped to
project_dir or the process CWD when workspace pool_data was unresolved. That
safety property is now covered by resolve_scope_path's contract — see
tests/unit/workspace/test_scope_path.py:
  - test_absent_manager_returns_none
  - test_unmaterialized_workspace_returns_none
  - test_workspace_level_address_has_no_pool_data
  - test_unknown_pool_returns_none_no_synthesis
"""
