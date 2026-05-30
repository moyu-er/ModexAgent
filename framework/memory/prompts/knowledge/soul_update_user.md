<knowledge_update_request>
  <instruction>
    Update SOUL.md based on the new facts below.
    Preserve all existing content unless explicitly contradicted.
    Focus only on facts marked [SOUL]. Ignore [USER], [MEMORY], [REMOVE] items
    unless a [REMOVE] entry references SOUL content.
  </instruction>
  <current_content file="SOUL.md">
{current_soul}
  </current_content>
  <analysis>
{new_facts}
  </analysis>
  <memory_context file="MEMORY.md">
{memory_context}
  </memory_context>
</knowledge_update_request>
