---
name: sql-query
description: Write, explain or review SQL queries and database migrations safely: correct joins, indexes, no destructive surprises.
license: Apache-2.0
---
1. Learn the schema first: migration files, models, or a read-only `\d`/`.schema`/`DESCRIBE` (never write to a database to explore it).
2. Write the query step by step: FROM/JOIN with the join keys named, WHERE, then GROUP BY/ORDER BY; parameters, never string-built values (SQL injection).
3. Check it on a small sample or a test database; compare row counts with what you expect.
4. Migrations: reversible (an up and a down), no data loss without saying so, large tables changed in batches; add indexes for new filters.
5. DELETE/UPDATE/DROP on real data: show the statement and its WHERE, and how many rows it would touch (a SELECT COUNT first) — the user runs it or approves it.
