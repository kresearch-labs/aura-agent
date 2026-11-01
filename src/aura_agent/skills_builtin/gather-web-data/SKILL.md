---
name: gather-web-data
description: Collect information from the web into a table or file: search, read the real pages, cite each value, respect sites' rules.
license: Apache-2.0
---
1. Define exactly which fields are needed and for which items (ask if unclear).
2. web_search to find the sources, web_crawl to read them — take values from the pages, never from memory.
3. Keep the source URL next to every value; mark missing values as missing, not guessed.
4. Be polite to sites: few pages, no login walls or paywalls, follow robots/terms; prefer official APIs or downloads.
5. Write the result as CSV/Markdown (write_file) and say what couldn't be found.
