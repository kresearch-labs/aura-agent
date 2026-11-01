---
name: write-docs
description: Write documentation for code: docstrings, API reference, a usage guide — accurate to the code, with runnable examples.
license: Apache-2.0
---
1. Read the code being documented and how it is actually called (search_files for uses).
2. Docstrings: what it does, parameters, return value, errors raised, in the project's existing style.
3. Guides: start with the shortest working example; then options; then edge cases.
4. Every example is run (run_command) or clearly marked untested.
5. Don't document behaviour you didn't see in the code; mark open questions for the user.
