```xml
<knowledge_update_request>
  <instruction>
    Update the persistent memory based on the new facts below.
    Preserve all existing information unless explicitly contradicted.
    Remove outdated or superseded facts.
    Output the COMPLETE new file content.
  </instruction>
  <current_content file="MEMORY.md">
{current_memory}
  </current_content>
  <new_facts>
{new_facts}
  </new_facts>
</knowledge_update_request>
```
