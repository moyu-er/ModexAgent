```xml
<knowledge_update_request>
  <instruction>
    Update the user profile based on the new facts below.
    Preserve all existing information unless explicitly contradicted.
    Fill in placeholder values with actual learned values.
    Output the COMPLETE new file content.
  </instruction>
  <current_content file="USER.md">
{current_user}
  </current_content>
  <new_facts>
{new_facts}
  </new_facts>
  <related_context file="MEMORY.md">
{memory_context}
  </related_context>
</knowledge_update_request>
```
