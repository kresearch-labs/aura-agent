---
name: edit-config-safely
description: Change JSON/YAML/TOML/INI configuration without breaking it: keep format and comments, validate after.
license: Apache-2.0
---
1. Read the whole file and find where the setting is documented (README, schema, the program's --help).
2. Change only the keys asked for; keep order, indentation, quoting style and comments (edit_file, not a rewrite).
3. Parse the file after the edit (python -c json/yaml/tomllib) — Aura's syntax note also catches broken JSON/TOML.
4. Environment-specific files (.env, production configs): never print secrets back; ask before changing production values.
5. Restart or reload only if the user asked; say what has to be restarted for the change to apply.
