---
name: ci-workflow
description: Write or fix a CI pipeline (GitHub Actions, GitLab CI): run tests and lint on every push, pinned actions, cached.
license: Apache-2.0
---
1. Find how the project is tested and linted locally (README, package.json scripts, pyproject, Makefile) — CI runs the same commands.
2. Read existing workflow files (.github/workflows/*.yml, .gitlab-ci.yml) before writing new ones.
3. Pin actions to a version (actions/checkout@v4), set the language version explicitly, cache dependencies.
4. Least privilege: `permissions: contents: read` unless more is needed; secrets only through the CI's secret store.
5. For a failing run the user pastes or links: read the failing step's log first, reproduce the command locally (run_command), fix the cause.
6. Validate the YAML (parse it); you can't run the CI itself — say so and what to watch on the first run.
