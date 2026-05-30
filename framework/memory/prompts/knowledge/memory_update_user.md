<knowledge_update_request>
  <instruction>
    Update MEMORY.md based on the new facts below.
    Preserve all existing information unless explicitly contradicted.
    Remove outdated or superseded facts.
    Focus only on facts marked [MEMORY]. Ignore [SOUL], [USER] items
    unless a [REMOVE] entry references MEMORY content.
    Apply all [REMOVE] directives that target MEMORY.md content.
  </instruction>
  <current_content file="MEMORY.md">
{current_memory}
  </current_content>
  <analysis>
{new_facts}
  </analysis>
</knowledge_update_request>
