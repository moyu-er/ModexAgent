```xml
<knowledge_update_request>
  <instruction>
    Update the soul profile based on the new facts below.
    Preserve all existing principles unless explicitly contradicted.
    Output the COMPLETE new file content.
  </instruction>
  <current_content file="SOUL.md">
{current_soul}
  </current_content>
  <new_facts>
{new_facts}
  </new_facts>
  <related_context file="MEMORY.md">
{memory_context}
  </related_context>
</knowledge_update_request>
```
