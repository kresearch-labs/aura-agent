---
name: diagram-from-code
description: Draw an architecture, flow or sequence diagram from the code as Mermaid text: what calls what, from the real code.
license: Apache-2.0
---
1. Decide what the diagram must answer (how a request flows, what depends on what, the states of an object).
2. Read the entry points and follow the calls (code_nav definition/references, search_files) — only draw what the code shows.
3. Write Mermaid (flowchart, sequenceDiagram, classDiagram or stateDiagram-v2); ten to twenty nodes; names from the code.
4. Put it in a ```mermaid block in a .md file (write_file) so it renders on GitHub.
5. Note what was left out and anything you inferred rather than saw.
