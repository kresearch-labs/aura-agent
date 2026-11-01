---
name: accessibility-review
description: Review a web page or component for accessibility (WCAG): labels, contrast, keyboard use, headings, alt text.
license: Apache-2.0
---
1. Read the markup (read_file) or open the page (browser_run snapshot — the accessibility tree shows names and roles).
2. Check: every image has alt text (empty for decoration), every input has a label, buttons and links have names.
3. Keyboard: everything reachable and usable with Tab/Enter/Space, visible focus, no traps.
4. Structure: one h1, headings in order, landmarks (main, nav), lists as lists; contrast of text against background.
5. Report each issue with where it is, why it matters and the fix; fix the clear ones in code if asked.
