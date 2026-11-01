---
name: write-dockerfile
description: Write or review a Dockerfile / docker compose file: small image, pinned base, no secrets, runs as a user.
license: Apache-2.0
---
1. Read what the app needs to run: its language version, dependency file, start command, ports (read_file, list_files).
2. Base image: an official, pinned tag (python:3.13-slim, node:22-alpine) — never `latest`.
3. Copy the dependency file first and install, then copy the code (build cache). Use a .dockerignore (.git, node_modules, .env).
4. No secrets in the image (no ENV with keys, no copied .env); pass them at run time.
5. Run as a non-root USER; EXPOSE the real port; one process per container; a HEALTHCHECK if it serves.
6. If Docker is installed, build it (run_command) and fix errors; if not, say the file is untested.
