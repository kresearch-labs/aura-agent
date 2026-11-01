---
name: api-design
description: Design or review an HTTP REST API: resources, status codes, errors, versioning, pagination, auth.
license: Apache-2.0
---
1. List the resources and what clients need to do with them before naming endpoints (nouns: /orders, /orders/{id}).
2. Methods: GET reads (no side effects), POST creates, PUT/PATCH change, DELETE removes; idempotency for retries.
3. Status codes that mean something: 200/201/204, 400 (bad input), 401/403, 404, 409 (conflict), 422, 429, 5xx.
4. One error shape everywhere ({"error": {"code", "message", "details"}}); validate input at the edge.
5. Lists are paginated (cursor or page+limit) and filterable; dates in ISO 8601 UTC; IDs as strings.
6. Versioning (/v1 or a header), auth (tokens, never in URLs), rate limits documented. Write or update the OpenAPI file if the project has one.
