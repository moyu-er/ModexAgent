<knowledge_update_request>
  <instruction>
    Update USER.md based on the new facts below.
    Preserve all existing information unless explicitly contradicted.
    Fill in placeholder values with actual learned values.
    Focus only on facts marked [USER]. Ignore [SOUL], [MEMORY], [REMOVE] items
    unless a [REMOVE] entry references USER content.
  </instruction>
  <current_content file="USER.md">
{current_user}
  </current_content>
  <analysis>
{new_facts}
  </analysis>
  <memory_context file="MEMORY.md">
{memory_context}
  </memory_context>
</knowledge_update_request>
