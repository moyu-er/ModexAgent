# Capability migration goldens

Goldens are machine captures, never hand-written expected values. Capture each
package on the parent commit of its migration wave and commit the generated
`<package>/<pool>.json` files:

- `todo`: T10's parent commit
- `experience`: T13's parent commit
- `subagents`: T15's parent commit

From the repository root, run:

```powershell
.venv\Scripts\python.exe -m tests.unit.scope.goldens.capture --package todo
```

The command compiles the shipped `examples/bot_project/config/scopes/bot.yml`
through the production `DefaultPlugin` plus project-plugin registry. Re-running
it on the same parent commit must produce no diff.

After the migration, call `capture_package_facets()`, parse each committed file
with `GoldenFile.model_validate_json()`, and compare each pool's `.root` mapping
through `assert_facets_equal()`. Pass a small, reasoned `Exemption` table only
for intentional representation changes; an unused exemption fails. Prompt
section content parity remains a separate byte comparison against the old
provider because pre-migration hardwired providers have no section id/order.
