---
name: performance-check
description: Find why something is slow, with numbers.
license: Apache-2.0
---
1. Measure first: time the slow operation (a small script or the project's benchmark) and write the number down.
2. Profile it (cProfile, `node --prof`, browser devtools) — find where the time actually goes.
3. Fix the biggest cost first; prefer algorithmic fixes over micro-tweaks.
4. Measure again with the same input; report before → after.
5. Don't trade correctness for speed — run the tests.
