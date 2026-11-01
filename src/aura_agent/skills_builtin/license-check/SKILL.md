---
name: license-check
description: Check the licences of a project's dependencies: list each, flag copyleft or unknown ones, without legal advice.
license: Apache-2.0
---
1. Find the dependency files (requirements.txt, pyproject, package.json, go.mod, Cargo.toml).
2. Get each package's licence from the installed metadata or its registry page (pip show, npm view <pkg> license, web_crawl the registry).
3. Group them: permissive (MIT, BSD, Apache-2.0), weak copyleft (LGPL, MPL), strong copyleft (GPL, AGPL), none/unknown.
4. Flag what may matter for how the project is distributed (AGPL for a service, GPL in shipped code, no licence at all).
5. Say clearly this is a list to check, not legal advice.
