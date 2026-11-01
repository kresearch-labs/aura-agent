---
name: add-type-hints
description: Add type hints to Python (or types to JavaScript/TypeScript) gradually, checked by a type checker.
license: Apache-2.0
---
1. Start with the public functions and data classes of one module, not everything at once.
2. Use precise types (list[str], dict[str, int], X | None); avoid Any unless you say why.
3. Run the project's type checker if it has one (mypy, pyright, tsc — via run_checks or run_command); Aura's language-server errors after edits help too.
4. Don't change behaviour while typing; a type error that shows a real bug is reported, then fixed separately.
5. Run the tests after each module.
